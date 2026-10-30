"""Regression checks for durable recovery and malformed persisted codes."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.obsidian_audit_recovery import main
from src.obsidian_policy import (
    AuditRecoveryAction,
    AuditRecoveryRequired,
    AuditRecoveryStatus,
    assess_and_record_request,
    inspect_audit_recovery,
    resolve_audit_recovery,
    verify_audit_log,
)


class TestObsidianAuditRecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audit.jsonl"
        self.marker = self.path.with_name(self.path.name + ".pending")
        self.now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }

    def create_present_recovery(self):
        real_fsync = os.fsync
        calls = 0

        def fail_log_sync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic log fsync failure")
            real_fsync(descriptor)

        with patch("src.obsidian_policy.audit.os.fsync", side_effect=fail_log_sync):
            with self.assertRaisesRegex(OSError, "synthetic log fsync failure"):
                assess_and_record_request(
                    self.request, audit_path=self.path, now_utc=self.now
                )
        report = inspect_audit_recovery(self.path)
        self.assertEqual(report.status, AuditRecoveryStatus.ENTRY_PRESENT)
        return report

    def test_present_recovery_retries_sync_before_removing_marker_without_reappend(self):
        report = self.create_present_recovery()
        original_log = self.path.read_bytes()
        real_fsync = os.fsync
        synced_descriptors = []

        def sync_existing_log(descriptor):
            self.assertTrue(self.marker.exists())
            self.assertEqual(os.fstat(descriptor).st_ino, self.path.stat().st_ino)
            self.assertEqual(self.path.read_bytes(), original_log)
            real_fsync(descriptor)
            synced_descriptors.append(descriptor)

        with patch("src.obsidian_policy.audit.os.fsync", side_effect=sync_existing_log):
            resolve_audit_recovery(
                self.path,
                expected_entry_hash=report.entry_hash,
                action=AuditRecoveryAction.ACKNOWLEDGE_PRESENT,
            )

        self.assertEqual(len(synced_descriptors), 1)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.path.read_bytes(), original_log)
        with self.assertRaises(OSError):
            os.fstat(synced_descriptors[0])
        self.assertTrue(verify_audit_log(self.path))

    def test_present_recovery_sync_failure_preserves_marker_and_policy_block(self):
        report = self.create_present_recovery()
        original_log = self.path.read_bytes()
        original_marker = self.marker.read_bytes()
        failed_descriptors = []

        def fail_sync(descriptor):
            failed_descriptors.append(descriptor)
            raise OSError("synthetic recovery fsync failure")

        with patch("src.obsidian_policy.audit.os.fsync", side_effect=fail_sync):
            with self.assertRaisesRegex(OSError, "synthetic recovery fsync failure"):
                resolve_audit_recovery(
                    self.path,
                    expected_entry_hash=report.entry_hash,
                    action=AuditRecoveryAction.ACKNOWLEDGE_PRESENT,
                )

        self.assertEqual(len(failed_descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(failed_descriptors[0])
        self.assertEqual(self.path.read_bytes(), original_log)
        self.assertEqual(self.marker.read_bytes(), original_marker)
        with patch("src.obsidian_policy.decision_service._assess_request") as assess:
            with self.assertRaises(AuditRecoveryRequired):
                assess_and_record_request(
                    self.request, audit_path=self.path, now_utc=self.now
                )
        assess.assert_not_called()

    def test_malformed_codes_in_marker_are_reported_as_conflict_without_mutation(self):
        self.check_malformed_codes(in_marker=True)

    def test_malformed_codes_in_log_are_reported_as_conflict_without_mutation(self):
        self.check_malformed_codes(in_marker=False)

    def check_malformed_codes(self, *, in_marker):
        report = self.create_present_recovery()
        original_log = self.path.read_bytes()
        original_marker = self.marker.read_bytes()
        for field, value in (
            ("decision_code", []),
            ("decision_code", {}),
            ("reason_codes", [[]]),
            ("reason_codes", [{}]),
        ):
            with self.subTest(field=field, value=value, in_marker=in_marker):
                self.path.write_bytes(original_log)
                self.marker.write_bytes(original_marker)
                target = self.marker if in_marker else self.path
                document = json.loads(target.read_bytes())
                entry = document["entry"] if in_marker else document
                entry[field] = value
                target.write_text(json.dumps(document) + "\n", encoding="utf-8")
                before = (self.path.read_bytes(), self.marker.read_bytes())

                inspected = inspect_audit_recovery(self.path)
                self.assertEqual(inspected.status, AuditRecoveryStatus.CONFLICT)
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main(["inspect", "--audit-path", str(self.path)])
                self.assertEqual(exit_code, 3)
                self.assertEqual(json.loads(output.getvalue())["status"], "conflict")
                with self.assertRaises(AuditRecoveryRequired):
                    resolve_audit_recovery(
                        self.path,
                        expected_entry_hash=report.entry_hash,
                        action=AuditRecoveryAction.ACKNOWLEDGE_PRESENT,
                    )
                self.assertEqual((self.path.read_bytes(), self.marker.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
