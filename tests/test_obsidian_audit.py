import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.obsidian_policy import (
    append_audit_entry,
    create_audit_entry,
    harden_audit_storage,
    verify_audit_log,
)
from src.obsidian_policy.audit import GENESIS_HASH
from src.obsidian_policy.core import assess_request


class TestObsidianAudit(unittest.TestCase):
    def setUp(self):
        self.request = {
            "purpose": "Review a private local configuration",
            "target_scope": "owned_system",
            "authorization_confirmed": True,
            "capabilities": ["review_user_provided_configuration"],
        }
        self.assessment = assess_request(self.request)

    def make_entry(self, event_id, timestamp, previous_hash=GENESIS_HASH):
        return create_audit_entry(
            self.request,
            self.assessment,
            event_id=event_id,
            timestamp_utc=timestamp,
            policy_version="test-policy-v1",
            previous_hash=previous_hash,
        )

    def test_entry_contains_only_minimized_fields_and_hashes_request(self):
        entry = self.make_entry(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
        )
        serialized = json.dumps(entry.to_dict(), sort_keys=True)

        self.assertNotIn(self.request["purpose"], serialized)
        self.assertNotIn("review_user_provided_configuration", serialized)
        self.assertEqual(len(entry.request_hash), 64)
        self.assertEqual(len(entry.entry_hash), 64)

    def test_two_entries_form_and_verify_a_chain(self):
        first = self.make_entry(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
        )
        second = self.make_entry(
            "00000000-0000-4000-8000-000000000002",
            "2026-09-26T12:00:01Z",
            first.entry_hash,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            append_audit_entry(path, first)
            append_audit_entry(path, second)

            self.assertTrue(verify_audit_log(path))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["previous_hash"], first.entry_hash)

    def test_content_tampering_is_detected(self):
        entry = self.make_entry(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            append_audit_entry(path, entry)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["policy_version"] = "tampered"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "hash does not match"):
                verify_audit_log(path)

    def test_broken_chain_and_incomplete_line_are_detected(self):
        first = self.make_entry(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
        )
        wrong_second = self.make_entry(
            "00000000-0000-4000-8000-000000000002",
            "2026-09-26T12:00:01Z",
            GENESIS_HASH,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            append_audit_entry(path, first)
            with self.assertRaisesRegex(ValueError, "previous_hash"):
                append_audit_entry(path, wrong_second)
            with path.open("ab") as stream:
                stream.write(b"not-complete")
            with self.assertRaisesRegex(ValueError, "incomplete final line"):
                verify_audit_log(path)

    def test_unknown_fields_and_unregistered_codes_fail_closed(self):
        entry = self.make_entry(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            harden_audit_storage(path)
            value = entry.to_dict()
            value["extra"] = True
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema"):
                verify_audit_log(path)

        bad_assessment = dict(self.assessment, reason_codes=["MADE_UP"])
        with self.assertRaisesRegex(ValueError, "registered codes"):
            create_audit_entry(
                self.request,
                bad_assessment,
                event_id="00000000-0000-4000-8000-000000000003",
                timestamp_utc="2026-09-26T12:00:02Z",
                policy_version="test-policy-v1",
            )


if __name__ == "__main__":
    unittest.main()
