import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.obsidian_policy as public_policy
from src.obsidian_policy import run_audited_config_analysis, verify_audit_log


class TestObsidianAuditedConfigAnalysis(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }
        self.config = {
            "debug": False,
            "tls_enabled": True,
            "logging_enabled": True,
            "allowed_hosts": ["app.test"],
            "admin_interface_public": False,
        }

    def test_result_is_released_only_after_audited_policy_approval(self):
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            assessment, analysis = run_audited_config_analysis(
                self.request,
                self.config,
                audit_path=audit_path,
                now_utc=self.now,
            )

            self.assertEqual(assessment["decision"], "approved")
            self.assertEqual(analysis["decision"], "completed")
            self.assertTrue(verify_audit_log(audit_path))

    def test_each_policy_nonapproval_is_audited_without_running_analyzer(self):
        cases = (
            (["obtain_credentials"], "rejected"),
            (["unknown_capability"], "needs_clarification"),
        )
        for capabilities, expected_decision in cases:
            with (
                self.subTest(decision=expected_decision),
                TemporaryDirectory() as directory,
            ):
                request = dict(self.request, capabilities=capabilities)
                audit_path = Path(directory) / "audit.jsonl"
                with patch(
                    "src.obsidian_policy.config_analysis_service._analyze_application_config"
                ) as analyzer:
                    assessment, analysis = run_audited_config_analysis(
                        request,
                        self.config,
                        audit_path=audit_path,
                        now_utc=self.now,
                    )

                self.assertEqual(assessment["decision"], expected_decision)
                self.assertIsNone(analysis)
                self.assertTrue(verify_audit_log(audit_path))
                analyzer.assert_not_called()

    def test_analyzer_rejection_is_released_after_policy_audit(self):
        config = dict(self.config, api_token="must-not-be-echoed")
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            assessment, analysis = run_audited_config_analysis(
                self.request,
                config,
                audit_path=audit_path,
                now_utc=self.now,
            )

            self.assertEqual(assessment["decision"], "approved")
            self.assertEqual(analysis["decision"], "rejected")
            self.assertNotIn("must-not-be-echoed", repr(analysis))
            self.assertTrue(verify_audit_log(audit_path))

    def test_audit_failure_prevents_analyzer_call_and_result_release(self):
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit.os.fsync",
                side_effect=OSError("synthetic passive audit failure"),
            ), patch(
                "src.obsidian_policy.config_analysis_service._analyze_application_config"
            ) as analyzer:
                with self.assertRaisesRegex(
                    OSError,
                    "synthetic passive audit failure",
                ):
                    run_audited_config_analysis(
                        self.request,
                        self.config,
                        audit_path=audit_path,
                        now_utc=self.now,
                    )

            analyzer.assert_not_called()

    def test_tampered_audit_chain_prevents_analyzer_call(self):
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            run_audited_config_analysis(
                self.request,
                self.config,
                audit_path=audit_path,
                now_utc=self.now,
            )
            content = audit_path.read_text(encoding="utf-8")
            audit_path.write_text(
                content.replace("obsidian-policy-v1", "tampered-policy"),
                encoding="utf-8",
            )

            with patch(
                "src.obsidian_policy.config_analysis_service._analyze_application_config"
            ) as analyzer:
                with self.assertRaisesRegex(
                    ValueError,
                    "audit entry hash does not match content",
                ):
                    run_audited_config_analysis(
                        self.request,
                        self.config,
                        audit_path=audit_path,
                        now_utc=self.now,
                    )

            analyzer.assert_not_called()

    def test_other_approved_passive_capability_cannot_reach_analyzer(self):
        request = dict(self.request, capabilities=["create_defensive_checklist"])
        with TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.config_analysis_service._analyze_application_config"
            ) as analyzer:
                with self.assertRaisesRegex(
                    ValueError,
                    "not bound to the configuration analysis capability",
                ):
                    run_audited_config_analysis(
                        request,
                        self.config,
                        audit_path=audit_path,
                        now_utc=self.now,
                    )

            self.assertTrue(verify_audit_log(audit_path))
            analyzer.assert_not_called()

    def test_raw_analyzer_is_not_publicly_exported(self):
        self.assertFalse(hasattr(public_policy, "analyze_application_config"))


if __name__ == "__main__":
    unittest.main()
