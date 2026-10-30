import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE
from src.forge_runtime.audit_acl_suite import run_audit_acl
from src.forge_runtime.host_suites import run_host_suite
from src.forge_runtime.task_runner import parse_task_spec
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestForgeAuditAclSuite(unittest.TestCase):
    def run_source(self, source):
        return run_audit_acl({"agent/audit_acl.py": source})

    def test_exact_module_passes_native_and_failure_cases(self):
        self.assertIsNone(self.run_source(EXPECTED_SOURCE))

    def test_owner_check_cannot_be_removed(self):
        changed = EXPECTED_SOURCE.replace("    _verify_owner(path.parent, user_sid)\n", "", 1)
        self.assertIn("exact frozen", self.run_source(changed))

    def test_rights_cannot_be_broadened(self):
        changed = EXPECTED_SOURCE.replace('    expected_trustees = {"SY", user_sid}', '    expected_trustees = {"SY", user_sid, "WD"}')
        self.assertIn("exact frozen", self.run_source(changed))

    def test_extra_code_is_rejected_before_native_calls(self):
        with patch("src.forge_runtime.audit_acl_suite._run_acl_cases") as cases:
            self.assertIn("exact frozen", self.run_source(EXPECTED_SOURCE + "\nraise RuntimeError('unexpected execution')\n"))
            cases.assert_not_called()

    def test_native_failures_do_not_pass_acceptance(self):
        with patch("src.forge_runtime.audit_acl_suite._run_acl_cases", side_effect=OSError("synthetic native failure")):
            self.assertIn("OSError", self.run_source(EXPECTED_SOURCE))

    def test_spec_has_exact_frozen_ast(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "examples/forge_audit_acl_SPEC.md").read_text(encoding="utf-8")
        source = "".join(block.split("\n```", 1)[0] + "\n" for block in spec.split("```python\n")[1:])
        self.assertEqual(ast.dump(ast.parse(source)), ast.dump(ast.parse(EXPECTED_SOURCE)))

    def test_task_and_host_suite_accept_allowed_module(self):
        root = Path(__file__).resolve().parents[1]
        task = parse_task_spec((root / "examples/forge_audit_acl.json").read_text(encoding="utf-8"))
        with TemporaryDirectory(dir=root) as directory:
            workspace = WorkspaceFileTools(Path(directory), task.task)
            workspace.write_text("agent/audit_acl.py", EXPECTED_SOURCE)
            passed, detail = run_host_suite("audit_acl_v1", workspace)
            self.assertTrue(passed, detail)


if __name__ == "__main__":
    unittest.main()
