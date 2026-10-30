import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.obsidian_policy import AuditAclError, assess_and_record_request
from src.obsidian_policy import audit_acl


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FOREIGN_OWNER_SID = "S-1-5-21-11111-22222-33333-1234"


@unittest.skipUnless(os.name == "nt", "Windows audit owner tests")
class TestObsidianAuditOwner(unittest.TestCase):
    def test_live_current_owner_is_verified_for_directory_file_and_marker(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            audit_acl.harden_audit_storage(path)
            marker = path.with_name(path.name + ".pending")
            marker.write_bytes(b"synthetic pending data")
            audit_acl.harden_audit_recovery_marker(path)

            user_sid = audit_acl._current_user_sid()
            for owned_path in (path.parent, path, marker):
                self.assertEqual(audit_acl._read_owner_sid(owned_path), user_sid)
            self.assertTrue(audit_acl.verify_audit_storage_acl(path))
            self.assertTrue(audit_acl.verify_audit_recovery_marker_acl(path))

    def test_foreign_directory_owner_blocks_initialization_before_acl_mutation(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            with patch.object(
                audit_acl, "_read_owner_sid", return_value=FOREIGN_OWNER_SID
            ), patch.object(audit_acl, "_set_strict_dacl") as set_dacl:
                with self.assertRaisesRegex(AuditAclError, "audit owner"):
                    audit_acl.harden_audit_storage(path)

            set_dacl.assert_not_called()
            self.assertFalse(path.exists())

    def test_foreign_existing_file_owner_blocks_before_parent_acl_mutation(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            path.write_bytes(b"preserve existing bytes")
            user_sid = audit_acl._current_user_sid()
            with patch.object(
                audit_acl,
                "_read_owner_sid",
                side_effect=lambda target: (
                    FOREIGN_OWNER_SID if target == path else user_sid
                ),
            ), patch.object(audit_acl, "_set_strict_dacl") as set_dacl:
                with self.assertRaisesRegex(AuditAclError, "audit owner"):
                    audit_acl.harden_audit_storage(path)

            set_dacl.assert_not_called()
            self.assertEqual(path.read_bytes(), b"preserve existing bytes")

    def test_changed_directory_or_file_owner_blocks_policy_without_acl_repair(self):
        request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }
        for changed_target in ("directory", "file"):
            with (
                self.subTest(changed_target=changed_target),
                TemporaryDirectory(dir=REPOSITORY_ROOT) as directory,
            ):
                path = Path(directory) / "audit.jsonl"
                audit_acl.harden_audit_storage(path)
                user_sid = audit_acl._current_user_sid()
                foreign_path = path.parent if changed_target == "directory" else path
                with patch.object(
                    audit_acl,
                    "_read_owner_sid",
                    side_effect=lambda target: (
                        FOREIGN_OWNER_SID if target == foreign_path else user_sid
                    ),
                ), patch.object(audit_acl, "_set_strict_dacl") as set_dacl, patch(
                    "src.obsidian_policy.decision_service._assess_request"
                ) as assess:
                    with self.assertRaisesRegex(AuditAclError, "audit owner"):
                        assess_and_record_request(request, audit_path=path)

                assess.assert_not_called()
                set_dacl.assert_not_called()
                self.assertEqual(path.read_bytes(), b"")

    def test_foreign_marker_owner_blocks_hardening_verification_and_policy(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            audit_acl.harden_audit_storage(path)
            marker = path.with_name(path.name + ".pending")
            marker.write_bytes(b"preserve pending data")
            audit_acl.harden_audit_recovery_marker(path)
            user_sid = audit_acl._current_user_sid()
            operations = (
                lambda: audit_acl.harden_audit_recovery_marker(path),
                lambda: audit_acl.verify_audit_recovery_marker_acl(path),
                lambda: assess_and_record_request({}, audit_path=path),
            )
            with patch.object(
                audit_acl,
                "_read_owner_sid",
                side_effect=lambda target: (
                    FOREIGN_OWNER_SID if target == marker else user_sid
                ),
            ), patch.object(audit_acl, "_set_strict_dacl") as set_dacl, patch(
                "src.obsidian_policy.decision_service._assess_request"
            ) as assess:
                for index, operation in enumerate(operations):
                    with self.subTest(operation=index):
                        with self.assertRaisesRegex(AuditAclError, "audit owner"):
                            operation()

            assess.assert_not_called()
            set_dacl.assert_not_called()
            self.assertEqual(marker.read_bytes(), b"preserve pending data")

    def test_owner_read_failure_blocks_initialization_without_acl_mutation(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            with patch.object(
                audit_acl,
                "_read_owner_sid",
                side_effect=AuditAclError("synthetic owner read failure"),
            ), patch.object(audit_acl, "_set_strict_dacl") as set_dacl:
                with self.assertRaisesRegex(AuditAclError, "owner read failure"):
                    audit_acl.harden_audit_storage(path)

            set_dacl.assert_not_called()
            self.assertFalse(path.exists())

    def test_system_owner_remains_allowed(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            audit_acl.harden_audit_storage(path)
            with patch.object(
                audit_acl, "_read_owner_sid", return_value="S-1-5-18"
            ):
                self.assertTrue(audit_acl.verify_audit_storage_acl(path))

    def test_valid_owner_does_not_hide_broad_or_unprotected_dacl(self):
        with TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "audit.jsonl"
            audit_acl.harden_audit_storage(path)
            user_sid = audit_acl._current_user_sid()
            read_dacl = audit_acl._read_dacl_sddl
            for target, flags in ((path.parent, "OICI"), (path, "")):
                allowed_aces = f"(A;{flags};FA;;;SY)(A;{flags};FA;;;{user_sid})"
                cases = (
                    "D:" + allowed_aces,
                    f"D:P(A;{flags};FA;;;SY)(A;{flags};FA;;;WD)",
                    "D:P" + allowed_aces + f"(A;{flags};FR;;;WD)",
                )
                for sddl in cases:
                    with self.subTest(target=target.name, sddl=sddl), patch.object(
                        audit_acl,
                        "_read_dacl_sddl",
                        side_effect=lambda candidate: (
                            sddl if candidate == target else read_dacl(candidate)
                        ),
                    ):
                        with self.assertRaisesRegex(AuditAclError, "audit DACL"):
                            audit_acl.verify_audit_storage_acl(path)


if __name__ == "__main__":
    unittest.main()
