import unittest
from unittest.mock import patch

from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE as ACL_SOURCE
from src.forge_runtime.audited_config_analysis_suite import _EXPECTED_SOURCE as FACADE_SOURCE
from src.forge_runtime.config_analysis_suite import _CONFIG_ANALYZER_TEMPLATE
from src.forge_runtime.durable_audit_contract import EXPECTED_SOURCE as AUDIT_SOURCE
from src.forge_runtime.passive_integration_verifier import verify_passive_integration


class TestForgePassiveIntegration(unittest.TestCase):
    def contents(self):
        return {
            "agent/audit.py": AUDIT_SOURCE, "agent/audit_acl.py": ACL_SOURCE,
            "agent/config_analysis_service.py": FACADE_SOURCE,
            "agent/config_analyzer.py": _CONFIG_ANALYZER_TEMPLATE,
        }

    def test_real_storage_analyzer_denials_failures_and_recovery(self):
        self.assertIsNone(verify_passive_integration(self.contents()))

    def test_premature_analysis_rejected_before_native_execution(self):
        contents = self.contents()
        contents["agent/config_analysis_service.py"] = FACADE_SOURCE.replace(
            '    assessment = assess_and_record_request(request, audit_path=audit_path)',
            '    analyze_application_config(config)\n    assessment = assess_and_record_request(request, audit_path=audit_path)',
        )
        with patch("src.forge_runtime.passive_integration_verifier.run_durable_audit") as execute:
            self.assertIn("exact frozen facade", verify_passive_integration(contents))
            execute.assert_not_called()

    def test_changed_storage_rejected_before_native_execution(self):
        contents = self.contents()
        contents["agent/audit.py"] += "\nraise RuntimeError('unexpected')\n"
        with patch("src.forge_runtime.passive_integration_verifier.run_durable_audit") as execute:
            self.assertIn("exact frozen audit", verify_passive_integration(contents))
            execute.assert_not_called()

    def test_analyzer_import_rejected_before_native_execution(self):
        contents = self.contents()
        contents["agent/config_analyzer.py"] += "\nimport os\n"
        with patch("src.forge_runtime.passive_integration_verifier.run_durable_audit") as execute:
            self.assertIsNotNone(verify_passive_integration(contents))
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
