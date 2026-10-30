import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.obsidian_policy import run_authorized_lab_scan, validate_execution_contract


class TestObsidianAuditExecutionGate(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Probe my loopback lab service",
            "target_scope": "isolated_local_lab",
            "authorization_confirmed": True,
            "capabilities": ["run_authorized_lab_scan"],
        }
        self.raw_contract = {
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
                "not_before_utc": "2026-09-27T11:59:00Z",
                "expires_at_utc": "2026-09-27T12:05:00Z",
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
        }

    def test_fsync_failure_prevents_every_socket_probe(self):
        contract = validate_execution_contract(
            self.raw_contract,
            bound_request=self.request,
            now_utc=self.now,
        )
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.active_scan._utc_now",
                return_value=self.now,
            ), patch(
                "src.obsidian_policy.audit.os.fsync",
                side_effect=OSError("synthetic durable audit failure"),
            ), patch(
                "src.obsidian_policy.active_scan._tcp_connect_probe"
            ) as probe:
                with self.assertRaisesRegex(
                    OSError,
                    "synthetic durable audit failure",
                ):
                    run_authorized_lab_scan(
                        self.request,
                        contract,
                        audit_path=audit_path,
                    )

        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
