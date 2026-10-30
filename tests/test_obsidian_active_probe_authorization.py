import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.obsidian_policy import ExecutionGuard, validate_execution_contract
from src.obsidian_policy.active_scan import _AuthorizedProbe, _tcp_connect_probe


class TestActiveProbeAuthorization(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Probe my loopback lab service",
            "target_scope": "isolated_local_lab",
            "authorization_confirmed": True,
            "capabilities": ["run_authorized_lab_scan"],
        }
        self.contract = validate_execution_contract(
            {
                "contract_id": "12345678-1234-5678-9234-567812345678",
                "contract_type": "authorized_lab_scan_v1",
                "capability": "run_authorized_lab_scan",
                "target_scope": "isolated_local_lab",
                "target": {
                    "kind": "ip_literal",
                    "address": "127.0.0.1",
                    "ports": [443],
                    "transport": "tcp",
                },
                "validity": {
                    "not_before_utc": "2026-09-26T11:59:00Z",
                    "expires_at_utc": "2026-09-26T12:05:00Z",
                },
                "limits": {
                    "max_connections": 1,
                    "max_concurrency": 1,
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
            },
            bound_request=self.request,
            now_utc=self.now,
        )

    def test_raw_values_cannot_reach_socket_helper(self):
        socket_factory = MagicMock()
        with patch(
            "src.obsidian_policy.active_scan.socket.socket", socket_factory
        ):
            with self.assertRaisesRegex(TypeError, "issued by ExecutionGuard"):
                _tcp_connect_probe(object(), timeout_seconds=1.0)
        socket_factory.assert_not_called()

    def test_probe_authorization_cannot_be_forged(self):
        with self.assertRaisesRegex(TypeError, "issued by ExecutionGuard"):
            _AuthorizedProbe(
                address="127.0.0.1",
                port=443,
                transport="tcp",
                _seal=object(),
            )

    @patch("src.obsidian_policy.active_scan._monotonic", return_value=100.0)
    @patch("src.obsidian_policy.active_scan._utc_now")
    def test_guard_authorization_is_single_use(self, utc_now, _monotonic):
        utc_now.return_value = self.now
        guard = ExecutionGuard(self.contract)
        status, authorization = guard.reserve_probe(
            address="127.0.0.1", port=443, transport="tcp"
        )
        self.assertIsNone(status)
        self.assertIsNotNone(authorization)

        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.connect_ex.return_value = 0
        with patch(
            "src.obsidian_policy.active_scan.socket.socket", return_value=connection
        ):
            self.assertTrue(
                _tcp_connect_probe(authorization, timeout_seconds=1.0)
            )
            with self.assertRaisesRegex(ValueError, "already been consumed"):
                _tcp_connect_probe(authorization, timeout_seconds=1.0)

        self.assertEqual(connection.connect_ex.call_count, 1)


if __name__ == "__main__":
    unittest.main()
