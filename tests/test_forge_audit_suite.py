import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime import SubagentTask, WorkspaceFileTools, parse_task_spec
from src.forge_runtime.host_suites import run_host_suite


AUDIT_SOURCE = '''import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

GENESIS_HASH = "0" * 64
ENTRY_FIELDS = frozenset({
    "event_id", "timestamp_utc", "policy_version", "decision_code",
    "reason_codes", "request_hash", "previous_hash", "entry_hash"
})
DECISION_CODES = frozenset({
    "REQUEST_APPROVED", "REQUEST_NEEDS_CLARIFICATION", "REQUEST_REJECTED"
})
REASON_CODES = frozenset({
    "REQUEST_TYPE_INVALID", "FIELD_NAME_INVALID", "FIELD_MISSING",
    "FIELD_UNKNOWN", "PURPOSE_INVALID", "TARGET_SCOPE_INVALID",
    "AUTHORIZATION_REQUIRED", "CAPABILITIES_INVALID",
    "CAPABILITY_ITEM_INVALID", "CAPABILITY_DUPLICATE",
    "CAPABILITY_PROHIBITED", "CAPABILITY_ACTIVE_CONTRACT_REQUIRED",
    "CAPABILITY_ACTIVE_CONTRACT_VALIDATED", "CAPABILITY_UNKNOWN",
    "REQUEST_WITHIN_DEFENSIVE_SCOPE"
})
MAX_REASON_CODES = 20

def create_audit_entry(request, assessment, event_id, timestamp_utc,
                       policy_version, previous_hash=GENESIS_HASH):
    _validate_metadata(event_id, timestamp_utc, policy_version, previous_hash)
    if type(request) is not dict:
        raise ValueError("audit request must be a dictionary")
    if type(assessment) is not dict:
        raise ValueError("audit assessment must be a dictionary")
    decision_code = assessment.get("decision_code")
    reason_codes = assessment.get("reason_codes")
    if type(decision_code) is not str or decision_code not in DECISION_CODES:
        raise ValueError("audit decision_code is not registered")
    if type(reason_codes) is not list or len(reason_codes) > MAX_REASON_CODES or any(
        type(code) is not str or code not in REASON_CODES for code in reason_codes
    ):
        raise ValueError("audit reason_codes must be registered codes")
    request_hash = _hash_json(request)
    unsigned = {
        "event_id": event_id,
        "timestamp_utc": timestamp_utc,
        "policy_version": policy_version,
        "decision_code": decision_code,
        "reason_codes": list(reason_codes),
        "request_hash": request_hash,
        "previous_hash": previous_hash,
    }
    entry_hash = _hash_json(unsigned)
    return {
        "event_id": event_id,
        "timestamp_utc": timestamp_utc,
        "policy_version": policy_version,
        "decision_code": decision_code,
        "reason_codes": list(reason_codes),
        "request_hash": request_hash,
        "previous_hash": previous_hash,
        "entry_hash": entry_hash,
    }

def verify_audit_chain(entries):
    if type(entries) is not list:
        raise ValueError("audit entries must be a list")
    previous_hash = GENESIS_HASH
    for index, entry in enumerate(entries):
        _validate_entry(entry, previous_hash)
        previous_hash = entry["entry_hash"]
    return True

def _validate_entry(entry, expected_previous):
    if type(entry) is not dict or set(entry) != ENTRY_FIELDS:
        raise ValueError("invalid audit schema")
    _validate_metadata(
        entry["event_id"], entry["timestamp_utc"],
        entry["policy_version"], entry["previous_hash"]
    )
    if entry["previous_hash"] != expected_previous:
        raise ValueError("audit hash chain is broken")
    decision_code = entry["decision_code"]
    if type(decision_code) is not str or decision_code not in DECISION_CODES:
        raise ValueError("audit decision_code is not registered")
    reason_codes = entry["reason_codes"]
    if type(reason_codes) is not list or len(reason_codes) > MAX_REASON_CODES or any(
        type(code) is not str or code not in REASON_CODES for code in reason_codes
    ):
        raise ValueError("audit reason_codes must be registered codes")
    _require_hash(entry["request_hash"], "request_hash")
    _require_hash(entry["entry_hash"], "entry_hash")
    unsigned = {
        "event_id": entry["event_id"],
        "timestamp_utc": entry["timestamp_utc"],
        "policy_version": entry["policy_version"],
        "decision_code": decision_code,
        "reason_codes": list(entry["reason_codes"]),
        "request_hash": entry["request_hash"],
        "previous_hash": entry["previous_hash"],
    }
    if _hash_json(unsigned) != entry["entry_hash"]:
        raise ValueError("audit entry hash does not match content")

def _validate_metadata(event_id, timestamp_utc, policy_version, previous_hash):
    if type(event_id) is not str:
        raise ValueError("event_id must be a canonical UUID")
    parsed_event_id = UUID(event_id)
    if str(parsed_event_id) != event_id:
        raise ValueError("event_id must be a canonical UUID")
    if (
        type(timestamp_utc) is not str
        or not timestamp_utc.endswith("Z")
        or len(timestamp_utc) != 20
    ):
        raise ValueError("timestamp_utc must end in Z")
    parsed_timestamp = datetime.fromisoformat(timestamp_utc[:-1] + "+00:00")
    if parsed_timestamp.tzinfo != timezone.utc or parsed_timestamp.microsecond:
        raise ValueError("timestamp_utc must use UTC whole-second precision")
    if type(policy_version) is not str or not policy_version.strip():
        raise ValueError("policy_version must be non-empty")
    _require_hash(previous_hash, "previous_hash")

def _require_hash(value, field):
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("invalid lowercase SHA-256 hash")

def _hash_json(value):
    encoded = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(",", ":")
    )
'''


def task_json():
    return json.dumps(
        {
            "task_id": "minimized-audit-chain",
            "objective": "Build a minimized in-memory audit chain",
            "allowed_files": ["agent/audit_chain.py"],
            "checks": [{"kind": "host_suite", "suite": "minimized_audit_chain_v1"}],
        }
    )


class TestForgeAuditSuite(unittest.TestCase):
    def test_complete_audit_chain_passes(self):
        passed, detail = self._run(AUDIT_SOURCE)
        self.assertTrue(passed, detail)
        self.assertEqual(detail, "host suite minimized_audit_chain_v1 passed")

    def test_registered_suite_parses(self):
        spec = parse_task_spec(task_json())
        self.assertEqual(spec.checks[0].suite, "minimized_audit_chain_v1")

    def test_rejects_unapproved_import_or_top_level_call(self):
        for changed in (
            "import os\n" + AUDIT_SOURCE,
            AUDIT_SOURCE + '\nopen("leak.txt", "w")\n',
        ):
            with self.subTest(changed=changed[-30:]):
                passed, _detail = self._run(changed)
                self.assertFalse(passed)

    def test_rejects_request_content_in_entry(self):
        changed = AUDIT_SOURCE.replace(
            '"request_hash": request_hash,\n        "previous_hash": previous_hash,\n        "entry_hash": entry_hash,',
            '"request_hash": request["purpose"],\n        "previous_hash": previous_hash,\n        "entry_hash": entry_hash,',
        )
        passed, detail = self._run(changed)
        self.assertFalse(passed)
        self.assertIn("request content", detail)

    def test_rejects_hash_bypass_and_unknown_schema_acceptance(self):
        changes = (
            AUDIT_SOURCE.replace(
                "if _hash_json(unsigned) != entry[\"entry_hash\"]:",
                "if False:",
            ),
            AUDIT_SOURCE.replace(
                "if type(entry) is not dict or set(entry) != ENTRY_FIELDS:",
                "if type(entry) is not dict:",
            ),
        )
        for changed in changes:
            with self.subTest():
                passed, detail = self._run(changed)
                self.assertFalse(passed)
                self.assertIn("audit behavior failed", detail)

    def test_rejects_aliased_and_subclassed_reason_codes(self):
        changes = (
            AUDIT_SOURCE.replace(
                '"reason_codes": list(reason_codes),',
                '"reason_codes": reason_codes,',
            ),
            AUDIT_SOURCE.replace(
                "type(reason_codes) is not list",
                "not isinstance(reason_codes, list)",
            ),
            AUDIT_SOURCE.replace(
                "type(code) is not str",
                "not isinstance(code, str)",
            ),
        )
        for changed in changes:
            with self.subTest():
                passed, detail = self._run(changed)
                self.assertFalse(passed)
                self.assertIn("audit behavior failed", detail)

    def test_rejects_rehashed_bad_request_hash_and_noncanonical_timestamp(self):
        changes = (
            AUDIT_SOURCE.replace(
                '    _require_hash(entry["request_hash"], "request_hash")',
                "    pass",
            ),
            AUDIT_SOURCE.replace(
                '        or not timestamp_utc.endswith("Z")\n',
                "",
            ).replace(
                "        or len(timestamp_utc) != 20\n",
                "",
            ),
            AUDIT_SOURCE.replace(
                "        or len(timestamp_utc) != 20\n",
                "",
            ),
        )
        for changed in changes:
            with self.subTest():
                passed, detail = self._run(changed)
                self.assertFalse(passed)
                self.assertIn("audit behavior failed", detail)

    def test_rejects_private_access_and_unbounded_loop(self):
        changes = (
            AUDIT_SOURCE.replace(
                "_validate_metadata(event_id, timestamp_utc, policy_version, previous_hash)",
                "request.__class__\n    _validate_metadata(event_id, timestamp_utc, policy_version, previous_hash)",
                1,
            ),
            AUDIT_SOURCE.replace(
                "_validate_metadata(event_id, timestamp_utc, policy_version, previous_hash)",
                "while True:\n        pass\n    _validate_metadata(event_id, timestamp_utc, policy_version, previous_hash)",
                1,
            ),
        )
        for changed in changes:
            with self.subTest():
                passed, _detail = self._run(changed)
                self.assertFalse(passed)

    def test_accepts_safe_explicit_empty_and_hash_checks(self):
        changed = AUDIT_SOURCE.replace(
            "    previous_hash = GENESIS_HASH\n    for index, entry in enumerate(entries):",
            "    if len(entries) == 0:\n"
            "        return True\n"
            "    previous_hash = GENESIS_HASH\n"
            "    for index, entry in enumerate(entries):",
        ).replace(
            '    _require_hash(previous_hash, "previous_hash")',
            "    if type(previous_hash) is not str or len(previous_hash) != 64 or not all(\n"
            '        character in "0123456789abcdef" for character in previous_hash\n'
            "    ):\n"
            '        raise ValueError("invalid previous hash")',
        )

        passed, detail = self._run(changed)

        self.assertTrue(passed, detail)

    @staticmethod
    def _run(source):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            (root / "agent" / "audit_chain.py").write_text(source, encoding="utf-8")
            task = SubagentTask("audit", "audit", ("agent/audit_chain.py",))
            return run_host_suite(
                "minimized_audit_chain_v1",
                WorkspaceFileTools(root, task),
            )


if __name__ == "__main__":
    unittest.main()
