import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.obsidian_policy import (
    AuditAclError,
    append_audit_entry,
    assess_and_record_request,
    create_audit_entry,
    harden_audit_storage,
    verify_audit_storage_acl,
)
from src.obsidian_policy.audit import GENESIS_HASH
from src.obsidian_policy import audit_acl
from src.obsidian_policy.core import assess_request


@unittest.skipUnless(os.name == "nt", "Windows DACL hardening tests")
class TestObsidianAuditAcl(unittest.TestCase):
    def make_entry(self):
        request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }
        return create_audit_entry(
            request,
            assess_request(request),
            event_id="00000000-0000-4000-8000-000000000001",
            timestamp_utc="2026-09-27T12:00:00Z",
            policy_version="test-policy-v1",
            previous_hash=GENESIS_HASH,
        )

    def test_live_hardening_creates_and_verifies_directory_and_file_dacls(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"

            harden_audit_storage(path)

            self.assertTrue(path.is_file())
            self.assertTrue(verify_audit_storage_acl(path))

    def test_workspace_volume_supports_live_audit_dacl_contract(self):
        repository_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=repository_root) as directory:
            path = Path(directory) / "audit.jsonl"

            harden_audit_storage(path)

            self.assertTrue(verify_audit_storage_acl(path))

    def test_broad_or_unprotected_dacl_fails_verification(self):
        cases = (
            "D:(A;;FA;;;SY)(A;;FA;;;S-1-5-21-test)",
            "D:P(A;;FA;;;SY)(A;;FA;;;WD)",
            "D:P(A;;FA;;;SY)(A;;FA;;;S-1-5-21-test)(A;;FR;;;WD)",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            harden_audit_storage(path)
            with patch(
                "src.obsidian_policy.audit_acl._current_user_sid",
                return_value="S-1-5-21-test",
            ):
                for sddl in cases:
                    with self.subTest(sddl=sddl), patch(
                        "src.obsidian_policy.audit_acl._read_dacl_sddl",
                        return_value=sddl,
                    ):
                        with self.assertRaises(AuditAclError):
                            verify_audit_storage_acl(path)

    def test_live_broadened_existing_file_blocks_operational_check(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            harden_audit_storage(path)
            user_sid = audit_acl._current_user_sid()
            audit_acl._set_dacl_sddl(
                path,
                f"D:P(A;;FA;;;SY)(A;;FA;;;{user_sid})(A;;FR;;;WD)",
            )

            with self.assertRaisesRegex(AuditAclError, "exactly two ACEs"):
                audit_acl.ensure_audit_storage_is_secure(path)

    def test_unsupported_environment_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch("src.obsidian_policy.audit_acl.os.name", "posix"):
                with self.assertRaisesRegex(AuditAclError, "Windows DACL support"):
                    harden_audit_storage(path)

    def test_acl_set_failure_prevents_file_creation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit_acl._set_strict_dacl",
                side_effect=AuditAclError("synthetic ACL set failure"),
            ):
                with self.assertRaisesRegex(
                    AuditAclError,
                    "synthetic ACL set failure",
                ):
                    harden_audit_storage(path)

            self.assertFalse(path.exists())

    def test_acl_failure_prevents_audit_write(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit.ensure_audit_storage_is_secure",
                side_effect=AuditAclError("synthetic ACL verification failure"),
            ), patch("src.obsidian_policy.audit.os.open") as audit_open:
                with self.assertRaisesRegex(
                    AuditAclError,
                    "synthetic ACL verification failure",
                ):
                    append_audit_entry(path, self.make_entry())

            audit_open.assert_not_called()

    def test_acl_failure_prevents_policy_evaluation(self):
        request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }
        with TemporaryDirectory() as directory, patch(
            "src.obsidian_policy.decision_service.ensure_audit_log_ready",
            side_effect=AuditAclError("synthetic startup ACL failure"),
        ), patch("src.obsidian_policy.decision_service._assess_request") as assess:
            with self.assertRaisesRegex(
                AuditAclError,
                "synthetic startup ACL failure",
            ):
                assess_and_record_request(
                    request,
                    audit_path=Path(directory) / "audit.jsonl",
                )

            assess.assert_not_called()

    def test_non_dedicated_directory_is_rejected_before_acl_change(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unrelated.log").write_text("keep", encoding="utf-8")
            with patch(
                "src.obsidian_policy.audit_acl._set_strict_dacl"
            ) as set_dacl:
                with self.assertRaisesRegex(AuditAclError, "must be dedicated"):
                    harden_audit_storage(root / "audit.jsonl")

            set_dacl.assert_not_called()

    def test_reparse_point_is_rejected_before_acl_application(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit_acl._reject_reparse_point",
                side_effect=AuditAclError(
                    "audit parent directory must not be a reparse point"
                ),
            ), patch("src.obsidian_policy.audit_acl._set_strict_dacl") as set_dacl:
                with self.assertRaisesRegex(AuditAclError, "reparse point"):
                    harden_audit_storage(path)

            set_dacl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
