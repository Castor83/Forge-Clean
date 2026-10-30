import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE as ACL
from src.forge_runtime.durable_audit_contract import EXPECTED_SOURCE as AUDIT
from src.forge_runtime.audited_config_analysis_suite import _EXPECTED_SOURCE as FACADE
from src.forge_runtime.config_analysis_suite import _CONFIG_ANALYZER_TEMPLATE as ANALYZER
from src.forge_runtime.policy_service_suite import (
    CORE_SOURCE, SERVICE_SOURCE, CORE_FILES, SERVICE_FILES, CORE_PARTS,
    SERVICE_PARTS, run_policy_service, verify_policy_service, _time_import,
)
from src.forge_runtime.task_runner import parse_task_spec


class TestPolicyServiceSuite(unittest.TestCase):
    def contents(self):
        return {
            **dict(zip(CORE_FILES + SERVICE_FILES, CORE_PARTS + SERVICE_PARTS)),
            "agent/core.py": CORE_SOURCE, "agent/decision_service.py": SERVICE_SOURCE,
            "agent/audit.py": AUDIT, "agent/audit_acl.py": ACL,
            "agent/config_analysis_service.py": FACADE, "agent/config_analyzer.py": ANALYZER,
        }

    def test_full_policy_service_and_six_module_behavior(self):
        self.assertIsNone(run_policy_service(self.contents()))

    def test_missing_audit_rejected_before_native_storage(self):
        contents = self.contents()
        tree = ast.parse(SERVICE_SOURCE)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "assess_and_record_request")
        function.body = [n for n in function.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "append_audit_assessment")]
        contents["agent/decision_service.py"] = ast.unparse(tree)
        with patch("src.forge_runtime.policy_service_suite.run_durable_audit") as native:
            self.assertIn("exact frozen AST", verify_policy_service(contents))
            native.assert_not_called()

    def test_policy_allow_bypass_rejected_before_native_storage(self):
        contents = self.contents()
        contents["agent/core.py"] = CORE_SOURCE.replace('"rejected",', '"approved",', 1)
        with patch("src.forge_runtime.policy_service_suite.run_durable_audit") as native:
            self.assertIn("exact frozen AST", verify_policy_service(contents))
            native.assert_not_called()

    def test_fragment_boundary_omission_rejected_before_loading(self):
        contents = self.contents()
        contents[CORE_FILES[1]] = "".join(CORE_PARTS[1].splitlines(True)[1:])
        with patch("src.forge_runtime.policy_service_suite.verify_policy_service") as execute:
            self.assertIn("first differing line", run_policy_service(contents))
            execute.assert_not_called()

    def test_spec_task_and_reference_sources_match_frozen_contract(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "examples/forge_policy_service_SPEC.md").read_text(encoding="utf-8")
        blocks = tuple(block.split("\n```", 1)[0] + "\n" for block in spec.split("```text\n")[1:])
        self.assertEqual(blocks, CORE_PARTS + SERVICE_PARTS)
        task = parse_task_spec((root / "examples/forge_policy_service.json").read_text())
        self.assertEqual(task.task.allowed_files, CORE_FILES + SERVICE_FILES)
        for name, source in (("core", CORE_SOURCE), ("decision_service", SERVICE_SOURCE)):
            self.assertEqual((root / f"src/obsidian_policy/{name}.py").read_text(encoding="utf-8"), source)

    def test_datetime_dependency_does_not_allow_arbitrary_imports(self):
        for name in ("os", "subprocess", "sys", "pathlib"):
            with self.assertRaises(ImportError):
                _time_import(name)
        with self.assertRaises(ImportError):
            _time_import("time", level=1)


if __name__ == "__main__":
    unittest.main()
