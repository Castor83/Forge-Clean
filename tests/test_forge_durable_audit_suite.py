import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE as ACL_SOURCE
from src.forge_runtime.durable_audit_contract import EXPECTED_SOURCE
from src.forge_runtime.durable_audit_suite import run_durable_audit
from src.forge_runtime.host_suites import run_host_suite
from src.forge_runtime.task_runner import parse_task_spec
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestForgeDurableAuditSuite(unittest.TestCase):
    def run_source(self, source=EXPECTED_SOURCE, acl=ACL_SOURCE):
        return run_durable_audit({"agent/audit.py": source, "agent/audit_acl.py": acl})

    def test_complete_storage_and_acl_pass_real_composed_cases(self):
        self.assertIsNone(self.run_source())

    def test_marker_write_and_sync_cannot_be_removed(self):
        changed = EXPECTED_SOURCE.replace("    _write_pending_intent(path, entry)\n", "", 1)
        self.assertNotEqual(changed, EXPECTED_SOURCE)
        self.assertIn("exact frozen storage", self.run_source(changed))

    def test_unvalidated_storage_never_reaches_native_acl_execution(self):
        with patch("src.forge_runtime.durable_audit_suite._run_acl_cases") as cases:
            self.assertIn("exact frozen storage", self.run_source(EXPECTED_SOURCE + "\nraise RuntimeError('unexpected')\n"))
            cases.assert_not_called()

    def test_modified_read_only_acl_is_rejected(self):
        changed = ACL_SOURCE.replace("    _verify_owner(path.parent, user_sid)\n", "", 1)
        self.assertIn("exact frozen Windows", self.run_source(acl=changed))

    def test_spec_has_exact_frozen_ast(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "examples/forge_durable_audit_SPEC.md").read_text(encoding="utf-8")
        source = "".join(block.split("\n```", 1)[0] + "\n" for block in spec.split("```python\n")[1:])
        self.assertEqual(ast.dump(ast.parse(source)), ast.dump(ast.parse(EXPECTED_SOURCE)))

    def test_task_read_only_dependency_is_used_by_host(self):
        root = Path(__file__).resolve().parents[1]
        task = parse_task_spec((root / "examples/forge_durable_audit.json").read_text(encoding="utf-8"))
        with TemporaryDirectory(dir=root) as directory:
            dependency = Path(directory) / "agent/audit_acl.py"
            dependency.parent.mkdir()
            dependency.write_text(ACL_SOURCE, encoding="utf-8")
            workspace = WorkspaceFileTools(Path(directory), task.task)
            workspace.write_text("agent/audit.py", EXPECTED_SOURCE)
            passed, detail = run_host_suite("durable_audit_v1", workspace)
            self.assertTrue(passed, detail)
            self.assertEqual(dependency.read_text(encoding="utf-8"), ACL_SOURCE)


if __name__ == "__main__":
    unittest.main()
