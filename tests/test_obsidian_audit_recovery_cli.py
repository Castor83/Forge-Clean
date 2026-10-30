import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.obsidian_audit_recovery import main
from src.obsidian_policy import (
    AuditRecoveryAction,
    AuditRecoveryReport,
    AuditRecoveryRequired,
    AuditRecoveryStatus,
    assess_and_record_request,
)


class TestObsidianAuditRecoveryCli(unittest.TestCase):
    def setUp(self):
        self.path = Path("V:/audit-state/audit.jsonl")
        self.entry_hash = "a" * 64

    def test_inspect_outputs_machine_readable_non_conflict_report(self):
        report = AuditRecoveryReport(
            status=AuditRecoveryStatus.ENTRY_PRESENT,
            event_id="5c7df556-24c3-4bdb-aec9-033ee15ffbd6",
            entry_hash=self.entry_hash,
            detail="pending entry is the verified end of the audit chain",
        )
        output = io.StringIO()

        with patch(
            "scripts.obsidian_audit_recovery.inspect_audit_recovery",
            return_value=report,
        ), redirect_stdout(output):
            exit_code = main(["inspect", "--audit-path", str(self.path)])

        self.assertEqual(exit_code, 0)
        self.assertIn('"status": "entry_present"', output.getvalue())
        self.assertIn(f'"entry_hash": "{self.entry_hash}"', output.getvalue())

    def test_inspect_conflict_has_distinct_exit_code(self):
        report = AuditRecoveryReport(
            status=AuditRecoveryStatus.CONFLICT,
            event_id=None,
            entry_hash=None,
            detail="recovery data is invalid: ValueError",
        )

        with patch(
            "scripts.obsidian_audit_recovery.inspect_audit_recovery",
            return_value=report,
        ), redirect_stdout(io.StringIO()):
            exit_code = main(["inspect", "--audit-path", str(self.path)])

        self.assertEqual(exit_code, 3)

    def test_resolve_passes_exact_hash_and_enum_action(self):
        output = io.StringIO()
        with patch(
            "scripts.obsidian_audit_recovery.resolve_audit_recovery"
        ) as resolve, redirect_stdout(output):
            exit_code = main(
                [
                    "resolve",
                    "--audit-path",
                    str(self.path),
                    "--expected-entry-hash",
                    self.entry_hash,
                    "--action",
                    "append_missing",
                ]
            )

        self.assertEqual(exit_code, 0)
        resolve.assert_called_once_with(
            self.path,
            expected_entry_hash=self.entry_hash,
            action=AuditRecoveryAction.APPEND_MISSING,
        )
        self.assertIn('"resolved": true', output.getvalue())

    def test_verify_checks_existing_chain_without_initializing_storage(self):
        with patch(
            "scripts.obsidian_audit_recovery.verify_audit_log",
            return_value=True,
        ) as verify, redirect_stdout(io.StringIO()):
            exit_code = main(["verify", "--audit-path", str(self.path)])

        self.assertEqual(exit_code, 0)
        verify.assert_called_once_with(self.path)

    def test_verify_missing_path_fails_without_creating_audit_file(self):
        repository_root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(dir=repository_root) as directory:
            audit_path = Path(directory) / "audit.jsonl"

            with redirect_stderr(io.StringIO()):
                exit_code = main(
                    ["verify", "--audit-path", str(audit_path)]
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse(audit_path.exists())

    def test_recovery_failure_stays_blocked_with_nonzero_exit(self):
        error = io.StringIO()
        with patch(
            "scripts.obsidian_audit_recovery.resolve_audit_recovery",
            side_effect=AuditRecoveryRequired("manual review"),
        ), redirect_stderr(error):
            exit_code = main(
                [
                    "resolve",
                    "--audit-path",
                    str(self.path),
                    "--expected-entry-hash",
                    self.entry_hash,
                    "--action",
                    "acknowledge_present",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("manual review", error.getvalue())

    def test_documented_fresh_process_workflow_resolves_present_entry(self):
        repository_root = Path(__file__).resolve().parents[1]
        request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }
        now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory(dir=repository_root) as directory:
            audit_path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit.os.fsync",
                side_effect=[None, OSError("synthetic log fsync failure")],
            ):
                with self.assertRaises(OSError):
                    assess_and_record_request(
                        request,
                        audit_path=audit_path,
                        now_utc=now,
                    )

            inspected = self.run_cli(
                repository_root,
                "inspect",
                "--audit-path",
                str(audit_path),
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            report = json.loads(inspected.stdout)
            self.assertEqual(report["status"], "entry_present")

            resolved = self.run_cli(
                repository_root,
                "resolve",
                "--audit-path",
                str(audit_path),
                "--expected-entry-hash",
                report["entry_hash"],
                "--action",
                "acknowledge_present",
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)

            verified = self.run_cli(
                repository_root,
                "verify",
                "--audit-path",
                str(audit_path),
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout), {"audit_ready": True})

    @staticmethod
    def run_cli(repository_root: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.obsidian_audit_recovery",
                *arguments,
            ],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
