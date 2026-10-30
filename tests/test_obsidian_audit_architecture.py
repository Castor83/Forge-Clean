import ast
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.obsidian_policy import (
    assess_and_record_request,
    run_authorized_lab_scan,
    validate_execution_contract,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "obsidian_policy"


class TestObsidianPolicyArchitecture(unittest.TestCase):
    def test_only_decision_service_imports_raw_policy_primitive(self):
        violations: list[str] = []
        for source_path in sorted(PACKAGE_ROOT.rglob("*.py")):
            if source_path == PACKAGE_ROOT / "decision_service.py":
                continue
            tree = ast.parse(
                source_path.read_text(encoding="utf-8"),
                filename=str(source_path),
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports_raw_function = (
                        node.module in {"core", "src.obsidian_policy.core"}
                        and any(alias.name == "assess_request" for alias in node.names)
                    )
                    imports_core_module = (
                        (
                            node.level == 1
                            and node.module is None
                            or node.level == 0
                            and node.module == "src.obsidian_policy"
                        )
                        and any(alias.name == "core" for alias in node.names)
                    )
                    if imports_raw_function or imports_core_module:
                        violations.append(f"{source_path.name}:{node.lineno}")
                elif isinstance(node, ast.Import) and any(
                    alias.name == "src.obsidian_policy.core" for alias in node.names
                ):
                    violations.append(f"{source_path.name}:{node.lineno}")

        self.assertEqual(
            violations,
            [],
            "raw policy imports outside decision_service.py: "
            + ", ".join(violations),
        )

    def test_only_config_analysis_service_imports_raw_analyzer(self):
        violations: list[str] = []
        allowed_path = PACKAGE_ROOT / "config_analysis_service.py"
        for source_path in sorted(PACKAGE_ROOT.rglob("*.py")):
            if source_path == allowed_path:
                continue
            tree = ast.parse(
                source_path.read_text(encoding="utf-8"),
                filename=str(source_path),
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imports_raw_function = (
                        node.module
                        in {
                            "config_analyzer",
                            "src.obsidian_policy.config_analyzer",
                        }
                        and any(
                            alias.name == "analyze_application_config"
                            for alias in node.names
                        )
                    )
                    imports_analyzer_module = (
                        (
                            node.level == 1
                            and node.module is None
                            or node.level == 0
                            and node.module == "src.obsidian_policy"
                        )
                        and any(alias.name == "config_analyzer" for alias in node.names)
                    )
                    if imports_raw_function or imports_analyzer_module:
                        violations.append(f"{source_path.name}:{node.lineno}")
                elif isinstance(node, ast.Import) and any(
                    alias.name == "src.obsidian_policy.config_analyzer"
                    for alias in node.names
                ):
                    violations.append(f"{source_path.name}:{node.lineno}")

        self.assertEqual(
            violations,
            [],
            "raw analyzer imports outside config_analysis_service.py: "
            + ", ".join(violations),
        )


class TestObsidianAuditChainExecutionGate(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
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
            },
            bound_request=self.request,
            now_utc=self.now,
        )

    def test_tampered_audit_chain_prevents_every_socket_probe(self):
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            assess_and_record_request(
                {
                    "purpose": "Review my local configuration",
                    "target_scope": "owned_system",
                    "authorization_confirmed": True,
                    "capabilities": ["review_user_provided_configuration"],
                },
                audit_path=audit_path,
                now_utc=self.now,
            )
            entry = json.loads(audit_path.read_text(encoding="utf-8"))
            entry["policy_version"] = "tampered-policy-version"
            audit_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

            with patch(
                "src.obsidian_policy.active_scan._utc_now",
                return_value=self.now,
            ), patch(
                "src.obsidian_policy.active_scan._tcp_connect_probe"
            ) as probe:
                with self.assertRaisesRegex(
                    ValueError,
                    "audit entry hash does not match content",
                ):
                    run_authorized_lab_scan(
                        self.request,
                        self.contract,
                        audit_path=audit_path,
                    )

            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
