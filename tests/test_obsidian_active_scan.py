import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import MagicMock, patch

from src.obsidian_policy import (
    ExecutionGuard,
    ExecutionStatusCode,
    run_authorized_lab_scan,
    validate_execution_contract,
)
from src.obsidian_policy.active_scan import _tcp_connect_probe


class TestObsidianActiveScan(unittest.TestCase):
    def setUp(self):
        self.audit_directory = TemporaryDirectory()
        self.addCleanup(self.audit_directory.cleanup)
        self.audit_path = Path(self.audit_directory.name) / "audit.jsonl"
        self.now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.raw = {
            "contract_id": "12345678-1234-5678-9234-567812345678",
            "contract_type": "authorized_lab_scan_v1",
            "capability": "run_authorized_lab_scan",
            "target_scope": "isolated_local_lab",
            "target": {
                "kind": "ip_literal",
                "address": "127.0.0.1",
                "ports": [8080, 443],
                "transport": "tcp",
            },
            "validity": {
                "not_before_utc": "2026-09-26T11:59:00Z",
                "expires_at_utc": "2026-09-26T12:05:00Z",
            },
            "limits": {
                "max_connections": 8,
                "max_concurrency": 2,
                "max_runtime_seconds": 20,
                "max_bytes_sent": 0,
            },
            "allowed_actions": ["tcp_connect_probe"],
            "approval_points": ["before_execution"],
            "abort_conditions": [
                "authorization_revoked",
                "deadline_reached",
                "host_policy_denied",
                "resource_limit_reached",
                "target_mismatch",
            ],
        }
        self.request = {
            "purpose": "Probe my loopback lab service",
            "target_scope": "isolated_local_lab",
            "authorization_confirmed": True,
            "capabilities": ["run_authorized_lab_scan"],
        }

    def contract(self, raw=None):
        return validate_execution_contract(
            raw or self.raw, bound_request=self.request, now_utc=self.now
        )

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    @patch("src.obsidian_policy.active_scan._tcp_connect_probe")
    def test_fixed_host_path_probes_only_bound_ports_without_payload_api(
        self, probe, utc_now, _monotonic
    ):
        utc_now.return_value = self.now
        probe.side_effect = [True, False]

        result = run_authorized_lab_scan(
            self.request,
            self.contract(),
            audit_path=self.audit_path,
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.status_code, "EXECUTION_COMPLETED")
        self.assertEqual(
            [(item.port, item.status) for item in result.probes],
            [(443, "open"), (8080, "closed")],
        )
        self.assertEqual(
            [
                (call.args[0].address, call.args[0].port)
                for call in probe.call_args_list
            ],
            [("127.0.0.1", 443), ("127.0.0.1", 8080)],
        )
        for call in probe.call_args_list:
            self.assertEqual(set(call.kwargs), {"timeout_seconds"})

    @patch("src.obsidian_policy.active_scan._utc_now")
    @patch("src.obsidian_policy.active_scan._tcp_connect_probe")
    def test_policy_denial_has_no_network_effect(self, probe, utc_now):
        utc_now.return_value = self.now
        request = dict(self.request, execution_contract={"validated": True})

        result = run_authorized_lab_scan(
            request,
            self.contract(),
            audit_path=self.audit_path,
        )

        self.assertEqual(result.status_code, "EXECUTION_HOST_POLICY_DENIED")
        probe.assert_not_called()

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    @patch("src.obsidian_policy.active_scan._tcp_connect_probe", return_value=False)
    def test_expiry_during_attempt_is_reported_before_another_attempt(
        self, probe, utc_now, _monotonic
    ):
        expired = datetime(2026, 9, 26, 12, 5, 0, tzinfo=timezone.utc)
        utc_now.side_effect = [self.now, self.now, self.now, expired]

        result = run_authorized_lab_scan(
            self.request,
            self.contract(),
            audit_path=self.audit_path,
        )

        self.assertEqual(result.status_code, "EXECUTION_DEADLINE_REACHED")
        self.assertEqual(result.attempts, 1)
        probe.assert_called_once()

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    @patch("src.obsidian_policy.active_scan._tcp_connect_probe")
    def test_socket_error_fails_closed_without_continuing(
        self, probe, utc_now, _monotonic
    ):
        utc_now.return_value = self.now
        probe.side_effect = OSError("synthetic socket failure")

        result = run_authorized_lab_scan(
            self.request,
            self.contract(),
            audit_path=self.audit_path,
        )

        self.assertEqual(result.status_code, "EXECUTION_INTERNAL_ERROR")
        self.assertEqual(result.attempts, 1)
        probe.assert_called_once()

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    def test_guard_rejects_every_target_deviation_before_reserving_attempt(
        self, utc_now, _monotonic
    ):
        utc_now.return_value = self.now
        guard = ExecutionGuard(self.contract())

        for deviation in (
            {"address": "127.0.0.2", "port": 443, "transport": "tcp"},
            {"address": "localhost", "port": 443, "transport": "tcp"},
            {"address": "127.0.0.1", "port": 22, "transport": "tcp"},
            {"address": "127.0.0.1", "port": 443, "transport": "udp"},
        ):
            with self.subTest(deviation=deviation):
                self.assertIs(
                    guard.authorize_probe(**deviation),
                    ExecutionStatusCode.TARGET_MISMATCH,
                )
        self.assertEqual(guard.attempts, 0)

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    @patch("src.obsidian_policy.active_scan._tcp_connect_probe")
    def test_connection_budget_aborts_before_excess_attempt(
        self, probe, utc_now, _monotonic
    ):
        utc_now.return_value = self.now
        raw = copy.deepcopy(self.raw)
        raw["target"]["ports"] = [80, 81]
        raw["limits"]["max_connections"] = 1
        probe.return_value = False

        result = run_authorized_lab_scan(
            self.request,
            self.contract(raw),
            audit_path=self.audit_path,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.status_code, "EXECUTION_RESOURCE_LIMIT_REACHED")
        self.assertEqual(result.attempts, 1)
        probe.assert_called_once()

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    @patch("src.obsidian_policy.active_scan._tcp_connect_probe")
    def test_host_revocation_aborts_before_first_attempt(
        self, probe, utc_now, _monotonic
    ):
        utc_now.return_value = self.now
        abort_event = Event()
        abort_event.set()

        result = run_authorized_lab_scan(
            self.request,
            self.contract(),
            audit_path=self.audit_path,
            abort_event=abort_event,
        )

        self.assertEqual(result.status_code, "EXECUTION_AUTHORIZATION_REVOKED")
        self.assertEqual(result.attempts, 0)
        probe.assert_not_called()

    def test_socket_implementation_uses_numeric_family_without_dns_or_send(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.connect_ex.return_value = 0
        with patch(
            "src.obsidian_policy.active_scan._utc_now", return_value=self.now
        ), patch(
            "src.obsidian_policy.active_scan._monotonic", return_value=100.0
        ), patch(
            "src.obsidian_policy.active_scan.socket.socket", return_value=connection
        ) as factory, patch(
            "src.obsidian_policy.active_scan.socket.getaddrinfo"
        ) as getaddrinfo:
            guard = ExecutionGuard(self.contract())
            status, authorization = guard.reserve_probe(
                address="127.0.0.1", port=443, transport="tcp"
            )
            self.assertIsNone(status)
            self.assertIsNotNone(authorization)
            result = _tcp_connect_probe(authorization, timeout_seconds=1.0)

        self.assertTrue(result)
        factory.assert_called_once_with(2, 1)
        connection.connect_ex.assert_called_once_with(("127.0.0.1", 443))
        getaddrinfo.assert_not_called()
        self.assertFalse(connection.send.called)
        self.assertFalse(connection.sendall.called)


if __name__ == "__main__":
    unittest.main()
