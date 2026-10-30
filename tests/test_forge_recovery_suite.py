import ast
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.forge_runtime.recovery_suite import _EXPECTED_SOURCE, run_audit_recovery
from src.forge_runtime.host_suites import run_host_suite
from src.forge_runtime.task_runner import parse_task_spec
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestForgeRecoverySuite(unittest.TestCase):
    def run_source(self, source):
        return run_audit_recovery({"agent/audit_recovery.py": source})

    def test_valid_recovery_passes_real_storage_and_failure_cases(self):
        self.assertIsNone(self.run_source(_EXPECTED_SOURCE))

    def test_host_failures_are_reported_as_failed_acceptance(self):
        with patch("src.forge_runtime.recovery_suite._run_storage_cases", side_effect=OSError("synthetic storage unavailable")):
            self.assertIn("OSError", self.run_source(_EXPECTED_SOURCE))

    def test_invalid_code_never_reaches_storage_cases(self):
        with patch("src.forge_runtime.recovery_suite._run_storage_cases") as cases:
            self.assertIn("exact inspection", self.run_source(_EXPECTED_SOURCE + "\nraise RuntimeError('unexpected execution')\n"))
            cases.assert_not_called()

    def test_rejects_removal_without_sync_or_without_append_verification(self):
        for removed in (
            "            os.fsync(descriptor)\n",
            "        if not verified or verified[-1] != pending:\n            raise AuditRecoveryRequired(\"appended recovery entry did not verify\")\n",
        ):
            with self.subTest(removed=removed):
                self.assertIn("exact inspection", self.run_source(_EXPECTED_SOURCE.replace(removed, "            pass\n" if "fsync" in removed else "")))

    def test_rejects_missing_hash_binding_and_early_marker_removal(self):
        changed = _EXPECTED_SOURCE.replace(
            "    if report.entry_hash != expected_entry_hash:\n        raise AuditRecoveryRequired(\"recovery entry hash does not match confirmation\")\n",
            "",
        )
        self.assertIn("exact inspection", self.run_source(changed))
        changed = _EXPECTED_SOURCE.replace("    report = inspect_audit_recovery(path)", "    _remove_pending_intent(path)\n    report = inspect_audit_recovery(path)")
        self.assertIn("exact inspection", self.run_source(changed))

    def test_syntax_error_is_rejected(self):
        self.assertIn("invalid Python syntax", self.run_source("def broken("))

    def test_spec_matches_fixed_contract(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "examples/forge_audit_recovery_SPEC.md").read_text(encoding="utf-8")
        code = spec.split("```python\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(ast.dump(ast.parse(code)), ast.dump(ast.parse(_EXPECTED_SOURCE)))

    def test_task_and_registered_host_suite_accept_generated_file(self):
        root = Path(__file__).resolve().parents[1]
        task = parse_task_spec((root / "examples/forge_audit_recovery.json").read_text(encoding="utf-8"))
        with TemporaryDirectory(dir=root) as directory:
            workspace = WorkspaceFileTools(Path(directory), task.task)
            workspace.write_text("agent/audit_recovery.py", _EXPECTED_SOURCE)
            passed, detail = run_host_suite("audit_recovery_v1", workspace)
            self.assertTrue(passed, detail)


if __name__ == "__main__":
    unittest.main()
