import unittest
from pathlib import Path

from src.forge_runtime.audited_config_analysis_suite import (
    _EXPECTED_SOURCE,
    run_audited_config_analysis,
)
from src.forge_runtime.host_suites import required_files_for_suite, run_host_suite
from src.forge_runtime.task_runner import parse_task_spec
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class TestAuditedConfigAnalysisSuite(unittest.TestCase):
    def run_source(self, source):
        return run_audited_config_analysis({"agent/config_analysis_service.py": source})

    def test_exact_facade_passes_behavioral_cases(self):
        self.assertIsNone(self.run_source(_EXPECTED_SOURCE))

    def test_spec_and_registered_task_match_contract(self):
        root = Path(__file__).resolve().parents[1]
        spec_text = (root / "examples/forge_audited_config_analysis_SPEC.md").read_text(encoding="utf-8")
        task = parse_task_spec((root / "examples/forge_audited_config_analysis.json").read_text(encoding="utf-8"))
        code_blocks = [part.split("\n```", 1)[0] for part in spec_text.split("```python\n")[1:]]
        self.assertEqual(code_blocks[0] + "\n\n" + code_blocks[1] + "\n" + code_blocks[2] + "\n", _EXPECTED_SOURCE)
        self.assertEqual(task.task.allowed_files, ("agent/config_analysis_service.py",))
        self.assertEqual(required_files_for_suite("audited_config_analysis_v1"), task.task.allowed_files)

    def test_host_suite_reads_allowed_file(self):
        from tempfile import TemporaryDirectory

        task = parse_task_spec((Path(__file__).resolve().parents[1] / "examples/forge_audited_config_analysis.json").read_text(encoding="utf-8"))
        with TemporaryDirectory() as temp:
            workspace = WorkspaceFileTools(Path(temp), task.task)
            workspace.write_text("agent/config_analysis_service.py", _EXPECTED_SOURCE)
            self.assertTrue(run_host_suite("audited_config_analysis_v1", workspace)[0])

    def test_rejects_analyzer_before_audit(self):
        changed = _EXPECTED_SOURCE.replace(
            "    assessment = assess_and_record_request(request, audit_path=audit_path)",
            "    analysis = analyze_application_config(config)\n"
            "    assessment = assess_and_record_request(request, audit_path=audit_path)",
        )
        self.assertIn("exact audited release", self.run_source(changed))

    def test_rejects_optional_or_swallowed_audit(self):
        for replacement in (
            "    assessment = assess_and_record_request(request, audit_path=audit_path) if audit_path else {'decision': 'approved'}",
            "    try:\n        assessment = assess_and_record_request(request, audit_path=audit_path)\n    except OSError:\n        assessment = {'decision': 'approved'}",
        ):
            with self.subTest(replacement=replacement):
                changed = _EXPECTED_SOURCE.replace(
                    "    assessment = assess_and_record_request(request, audit_path=audit_path)",
                    replacement,
                )
                self.assertIn("exact audited release", self.run_source(changed))

    def test_rejects_denial_before_audit_and_missing_capability_gate(self):
        early_return = _EXPECTED_SOURCE.replace(
            "    assessment = assess_and_record_request(request, audit_path=audit_path)",
            "    if request.get('deny'):\n        return {'decision': 'rejected'}, None\n"
            "    assessment = assess_and_record_request(request, audit_path=audit_path)",
        )
        unchecked = _EXPECTED_SOURCE.replace(
            "    if type(request) is not dict or request.get(\"capabilities\") != [\"review_user_provided_configuration\"]:\n"
            "        raise ValueError(\"request is not bound to the configuration analysis capability\")\n",
            "",
        )
        self.assertIn("exact audited release", self.run_source(early_return))
        self.assertIn("exact audited release", self.run_source(unchecked))


if __name__ == "__main__":
    unittest.main()
