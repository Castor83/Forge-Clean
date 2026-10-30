import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.obsidian_policy as public_policy
from src.obsidian_policy import (
    assess_and_record_request,
    harden_audit_storage,
    verify_audit_log,
)


class TestObsidianAuditedDecision(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
        self.request = {
            "purpose": "Review my local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }

    def test_public_release_boundary_writes_before_returning(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"

            result = assess_and_record_request(
                self.request,
                audit_path=path,
                now_utc=self.now,
            )

            self.assertEqual(result["decision"], "approved")
            self.assertTrue(verify_audit_log(path))
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["decision_code"], "REQUEST_APPROVED")
            self.assertNotIn(self.request["purpose"], path.read_text(encoding="utf-8"))

    def test_every_decision_kind_is_durably_chained(self):
        requests = (
            self.request,
            dict(self.request, capabilities=["obtain_credentials"]),
            dict(self.request, capabilities=["unknown_capability"]),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            decisions = [
                assess_and_record_request(
                    request,
                    audit_path=path,
                    now_utc=self.now,
                )["decision"]
                for request in requests
            ]

            self.assertEqual(decisions, ["approved", "rejected", "needs_clarification"])
            self.assertTrue(verify_audit_log(path))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_fsync_failure_prevents_result_release(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            with patch(
                "src.obsidian_policy.audit.os.fsync",
                side_effect=OSError("synthetic fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic fsync failure"):
                    assess_and_record_request(
                        self.request,
                        audit_path=path,
                        now_utc=self.now,
                    )

    def test_invalid_existing_log_prevents_result_release(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            harden_audit_storage(path)
            path.write_bytes(b"incomplete")

            with self.assertRaisesRegex(ValueError, "incomplete final line"):
                assess_and_record_request(
                    self.request,
                    audit_path=path,
                    now_utc=self.now,
                )

    def test_raw_policy_primitive_is_not_publicly_exported(self):
        self.assertFalse(hasattr(public_policy, "assess_request"))


if __name__ == "__main__":
    unittest.main()
