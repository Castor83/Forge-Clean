import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime import SubagentTask, WorkspaceFileTools
from src.forge_runtime.host_suites import run_host_suite
from src.forge_runtime.jsonl_audit_suite import run_jsonl_audit_log
from tests.test_forge_audit_suite import AUDIT_SOURCE


AUDIT_LOG_SOURCE = '''import json
import os
from pathlib import Path
from .audit_chain import ENTRY_FIELDS, GENESIS_HASH, verify_audit_chain

def append_audit_entry(path, entry):
    if not isinstance(path, Path):
        raise ValueError("audit path must be a pathlib.Path")
    if type(entry) is not dict or set(entry) != ENTRY_FIELDS:
        raise ValueError("entry must use the exact audit schema")
    if not path.parent.is_dir():
        raise ValueError("audit parent directory must exist")
    entries = _read_entries(path)
    expected_previous = entries[-1]["entry_hash"] if entries else GENESIS_HASH
    if entry["previous_hash"] != expected_previous:
        raise ValueError("audit previous_hash does not match the verified chain")
    verify_audit_chain(entries + [entry])
    line = _canonical_json(entry).encode("utf-8") + b"\\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, line)
        if written != len(line):
            raise OSError("incomplete audit write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def verify_audit_log(path):
    _read_entries(path)
    return True

def _read_entries(path):
    if not isinstance(path, Path):
        raise ValueError("audit path must be a pathlib.Path")
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError("audit path must be a file")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\\n"):
        raise ValueError("audit log has an incomplete final line")
    entries = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid audit JSON")
        if type(value) is not dict or set(value) != ENTRY_FIELDS:
            raise ValueError("invalid audit schema")
        entries.append(value)
    verify_audit_chain(entries)
    return entries

def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
'''


class TestForgeJsonlAuditSuite(unittest.TestCase):
    def test_complete_jsonl_persistence_passes(self):
        self.assertIsNone(
            run_jsonl_audit_log(
                {
                    "agent/audit_chain.py": AUDIT_SOURCE,
                    "agent/audit_log.py": AUDIT_LOG_SOURCE,
                }
            )
        )

    def test_registered_suite_reads_verified_dependency_as_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            (root / "agent" / "audit_chain.py").write_text(
                AUDIT_SOURCE,
                encoding="utf-8",
            )
            (root / "agent" / "audit_log.py").write_text(
                AUDIT_LOG_SOURCE,
                encoding="utf-8",
            )
            task = SubagentTask(
                "jsonl-audit",
                "persist audit entries",
                ("agent/audit_log.py",),
                ("agent/audit_chain.py",),
            )
            workspace = WorkspaceFileTools(root, task)

            passed, detail = run_host_suite("jsonl_audit_log_v1", workspace)

        self.assertTrue(passed, detail)
        self.assertEqual(detail, "host suite jsonl_audit_log_v1 passed")

    def test_rejects_non_append_open_flags(self):
        changed = AUDIT_LOG_SOURCE.replace(
            "os.O_WRONLY | os.O_CREAT | os.O_APPEND",
            "os.O_WRONLY | os.O_CREAT",
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn("O_APPEND", failure)

    def test_rejects_a_second_write_even_when_it_writes_no_bytes(self):
        changed = AUDIT_LOG_SOURCE.replace(
            "written = os.write(descriptor, line)",
            'os.write(descriptor, b"")\n        written = os.write(descriptor, line)',
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn("append must use O_APPEND", failure)

    def test_rejects_close_outside_the_finally_block(self):
        changed = AUDIT_LOG_SOURCE.replace(
            "    finally:\n        os.close(descriptor)",
            "    finally:\n        pass\n    os.close(descriptor)",
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn("close in finally", failure)

    def test_rejects_literal_escape_path_before_execution(self):
        changed = AUDIT_LOG_SOURCE.replace(
            "def verify_audit_log(path):",
            'def verify_audit_log(path):\n    forbidden = "C:/outside"',
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn("literal filesystem path", failure)

    def test_reports_each_unsupported_call_with_its_line(self):
        changed = AUDIT_LOG_SOURCE.replace(
            "raw = path.read_bytes()",
            'raw = open(path, "rb").read()',
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn("forbidden name open", failure)
        self.assertIn("call is outside the contract", failure)

    def test_reports_structural_failures_from_all_functions(self):
        changed = AUDIT_LOG_SOURCE.replace(
            'raise ValueError("entry must use the exact audit schema")',
            'raise TypeError("entry must use the exact audit schema")',
        ).replace(
            "def verify_audit_log(path):\n    _read_entries(path)",
            "def verify_audit_log(path):\n    path.open()\n    _read_entries(path)",
        ).replace(
            "raw = path.read_bytes()",
            'raw = open(path, "rb").read()',
        ).replace(
            "def _canonical_json(value):\n    return json.dumps(",
            "def _canonical_json(value):\n    print(value)\n    return json.dumps(",
        )

        failure = run_jsonl_audit_log(
            {
                "agent/audit_chain.py": AUDIT_SOURCE,
                "agent/audit_log.py": changed,
            }
        )

        self.assertIn("TypeError", failure)
        self.assertIn("path.open()", failure)
        self.assertIn("forbidden name open", failure)
        self.assertIn("print(value)", failure)


if __name__ == "__main__":
    unittest.main()
