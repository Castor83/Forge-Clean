import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime import SubagentTask, WorkspaceFileTools, parse_task_spec
from src.forge_runtime.host_suites import run_host_suite


REFERENCE_PATH = Path("src/obsidian_policy/config_analyzer.py")


def task_json():
    return json.dumps(
        {
            "task_id": "redacted-config-analysis",
            "objective": "Build fail-closed local config analysis",
            "allowed_files": ["agent/config_analyzer.py"],
            "checks": [
                {"kind": "host_suite", "suite": "redacted_config_analysis_v1"}
            ],
        }
    )


class TestForgeConfigAnalysisSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = REFERENCE_PATH.read_text(encoding="utf-8")

    def test_reference_analyzer_passes(self):
        passed, detail = self._run(self.source)

        self.assertTrue(passed, detail)
        self.assertEqual(
            detail,
            "host suite redacted_config_analysis_v1 passed",
        )

    def test_registered_suite_parses_and_requires_exact_file(self):
        spec = parse_task_spec(task_json())

        self.assertEqual(spec.checks[0].suite, "redacted_config_analysis_v1")
        self.assertEqual(spec.task.allowed_files, ("agent/config_analyzer.py",))

    def test_rejects_top_level_io_even_if_analysis_is_unchanged(self):
        passed, detail = self._run('open("leak.txt", "w")\n' + self.source)

        self.assertFalse(passed)
        self.assertIn("may contain only", detail)

    def test_rejects_redaction_bypass(self):
        changed = self.source.replace(
            "if field in config and config[field] != REDACTION_MARKER",
            "if False",
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("behavior failed", detail)

    def test_rejects_secret_echo(self):
        changed = self.source.replace(
            'f"sensitive field must contain exact REDACTED marker: {field}"',
            'f"bad value {config[field]} in {field}"',
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("sensitive value", detail)

    def test_rejects_analysis_before_redaction(self):
        changed = self.source.replace(
            "redaction_reasons = _validate_redaction_contract(config)",
            "redaction_reasons = []",
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("behavior failed", detail)

    def test_rejects_extra_result_field(self):
        changed = self.source.replace(
            '"decision": "completed",\n        "findings": findings,',
            '"decision": "completed",\n        "debug_dump": config,\n        "findings": findings,',
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("invalid result schema", detail)

    def test_rejects_input_mutation_method(self):
        changed = self.source.replace(
            "redaction_reasons = _validate_redaction_contract(config)",
            "config.clear()\n    redaction_reasons = _validate_redaction_contract(config)",
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("method call is not allowed", detail)

    def test_rejects_private_attribute_access(self):
        changed = self.source.replace(
            "redaction_reasons = _validate_redaction_contract(config)",
            "config.__class__\n    redaction_reasons = _validate_redaction_contract(config)",
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("private attribute", detail)

    def test_rejects_unbounded_loop(self):
        changed = self.source.replace(
            "redaction_reasons = _validate_redaction_contract(config)",
            "while True:\n        pass\n    "
            "redaction_reasons = _validate_redaction_contract(config)",
        )

        passed, detail = self._run(changed)

        self.assertFalse(passed)
        self.assertIn("forbidden control", detail)

    @staticmethod
    def _run(source):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent").mkdir()
            (root / "agent" / "config_analyzer.py").write_text(
                source,
                encoding="utf-8",
            )
            task = SubagentTask(
                "redacted-config-analysis",
                "build analyzer",
                ("agent/config_analyzer.py",),
            )
            workspace = WorkspaceFileTools(root, task)
            return run_host_suite("redacted_config_analysis_v1", workspace)


if __name__ == "__main__":
    unittest.main()
