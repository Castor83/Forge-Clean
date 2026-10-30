import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.obsidian_policy import (
    AuditRecoveryAction,
    AuditRecoveryRequired,
    AuditRecoveryStatus,
    assess_and_record_request,
    inspect_audit_recovery,
    resolve_audit_recovery,
    verify_audit_log,
)


class TestObsidianAuditRecovery(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }

    def test_normal_append_clears_pending_marker(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"

            result = assess_and_record_request(
                self.request,
                audit_path=path,
                now_utc=self.now,
            )

            self.assertEqual(result["decision"], "approved")
            self.assertFalse(self.pending_path(path).exists())
            self.assertTrue(verify_audit_log(path))

    def test_workspace_volume_supports_write_ahead_protocol(self):
        repository_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=repository_root) as directory:
            path = Path(directory) / "audit.jsonl"

            assess_and_record_request(
                self.request,
                audit_path=path,
                now_utc=self.now,
            )

            self.assertFalse(self.pending_path(path).exists())
            self.assertTrue(verify_audit_log(path))

    def test_unclear_log_fsync_blocks_new_policy_before_evaluation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit.os.fsync",
                side_effect=[None, OSError("synthetic log fsync failure")],
            ):
                with self.assertRaisesRegex(OSError, "synthetic log fsync failure"):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )

            self.assertTrue(self.pending_path(path).is_file())
            self.assertEqual(
                inspect_audit_recovery(path).status,
                AuditRecoveryStatus.ENTRY_PRESENT,
            )
            with patch(
                "src.obsidian_policy.decision_service._assess_request"
            ) as assess:
                with self.assertRaisesRegex(
                    AuditRecoveryRequired,
                    "recovery is required",
                ):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )

            assess.assert_not_called()

    def test_directory_flush_failure_prevents_audit_append(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit.flush_audit_directory",
                side_effect=OSError("synthetic directory flush failure"),
            ), patch("src.obsidian_policy.audit._append_entry_line") as append_line:
                with self.assertRaisesRegex(
                    OSError,
                    "synthetic directory flush failure",
                ):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )

            append_line.assert_not_called()
            self.assertTrue(self.pending_path(path).exists())

    def test_present_entry_requires_hash_bound_acknowledgement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            self.create_present_recovery(path)
            report = inspect_audit_recovery(path)

            with self.assertRaisesRegex(AuditRecoveryRequired, "does not match"):
                resolve_audit_recovery(
                    path,
                    expected_entry_hash="0" * 64,
                    action=AuditRecoveryAction.ACKNOWLEDGE_PRESENT,
                )
            self.assertTrue(self.pending_path(path).exists())

            resolve_audit_recovery(
                path,
                expected_entry_hash=report.entry_hash,
                action=AuditRecoveryAction.ACKNOWLEDGE_PRESENT,
            )

            self.assertFalse(self.pending_path(path).exists())
            self.assertTrue(verify_audit_log(path))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

            assess_and_record_request(
                self.request,
                audit_path=path,
                now_utc=self.now,
            )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_fresh_process_detects_persisted_recovery_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            self.create_present_recovery(path)
            code = (
                "from pathlib import Path; "
                "from src.obsidian_policy import inspect_audit_recovery; "
                "import sys; "
                "print(inspect_audit_recovery(Path(sys.argv[1])).status.value)"
            )

            completed = subprocess.run(
                [sys.executable, "-c", code, str(path)],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "entry_present")

    def test_absent_entry_requires_explicit_append_action(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit._append_entry_line",
                side_effect=OSError("synthetic pre-append failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic pre-append failure"):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )
            report = inspect_audit_recovery(path)
            self.assertEqual(report.status, AuditRecoveryStatus.ENTRY_ABSENT)

            with self.assertRaisesRegex(AuditRecoveryRequired, "explicit append"):
                resolve_audit_recovery(
                    path,
                    expected_entry_hash=report.entry_hash,
                    action=AuditRecoveryAction.ACKNOWLEDGE_PRESENT,
                )

            resolve_audit_recovery(
                path,
                expected_entry_hash=report.entry_hash,
                action=AuditRecoveryAction.APPEND_MISSING,
            )
            self.assertFalse(self.pending_path(path).exists())
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
            self.assertTrue(verify_audit_log(path))

    def test_conflict_remains_blocked_and_cannot_be_resolved(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit._append_entry_line",
                side_effect=OSError("synthetic pre-append failure"),
            ):
                with self.assertRaises(OSError):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )
            path.write_bytes(b"incomplete")

            report = inspect_audit_recovery(path)

            self.assertEqual(report.status, AuditRecoveryStatus.CONFLICT)
            with self.assertRaisesRegex(AuditRecoveryRequired, "manual review"):
                resolve_audit_recovery(
                    path,
                    expected_entry_hash=report.entry_hash,
                    action=AuditRecoveryAction.APPEND_MISSING,
                )
            self.assertTrue(self.pending_path(path).exists())

    def test_missing_main_log_with_pending_marker_is_a_conflict(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit._append_entry_line",
                side_effect=OSError("synthetic pre-append failure"),
            ):
                with self.assertRaises(OSError):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )
            path.unlink()

            report = inspect_audit_recovery(path)

            self.assertEqual(report.status, AuditRecoveryStatus.CONFLICT)
            self.assertFalse(path.exists())
            with self.assertRaisesRegex(AuditRecoveryRequired, "manual review"):
                resolve_audit_recovery(
                    path,
                    expected_entry_hash=report.entry_hash,
                    action=AuditRecoveryAction.APPEND_MISSING,
                )

    def create_present_recovery(self, path: Path) -> None:
        with patch(
            "src.obsidian_policy.audit.os.fsync",
            side_effect=[None, OSError("synthetic log fsync failure")],
        ):
            with self.assertRaises(OSError):
                assess_and_record_request(
                    self.request,
                    audit_path=path,
                    now_utc=self.now,
                )

    @staticmethod
    def pending_path(path: Path) -> Path:
        return path.with_name(path.name + ".pending")


if __name__ == "__main__":
    unittest.main()
