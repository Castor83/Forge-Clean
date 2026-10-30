import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime import (
    SubagentTask,
    WorkspaceFileTools,
    parse_task_spec,
    run_host_suite,
)
from src.forge_runtime.advanced_policy_suite import (
    _CAPABILITIES_TEMPLATE,
    _DECISIONS_TEMPLATE,
    _POLICY_TEMPLATE,
)


class TestAdvancedDefensivePolicySuite(unittest.TestCase):
    def test_complete_coded_multi_capability_policy_passes(self):
        self._assert_passes(
            _CAPABILITIES_TEMPLATE,
            _DECISIONS_TEMPLATE,
            _POLICY_TEMPLATE,
        )

    def test_host_cases_include_active_capability_unknown_to_policy_source(self):
        self.assertNotIn("new_active_test_capability", _POLICY_TEMPLATE)
        self._assert_passes(
            _CAPABILITIES_TEMPLATE,
            _DECISIONS_TEMPLATE,
            _POLICY_TEMPLATE,
        )

    def test_keyword_capability_constructors_are_normalized(self):
        source = _CAPABILITIES_TEMPLATE
        replacements = (
            (
                '"explain_security_concepts", SecurityClass.PASSIVE',
                'identifier="explain_security_concepts", '
                'security_class=SecurityClass.PASSIVE',
            ),
            (
                '"review_user_provided_configuration", SecurityClass.PASSIVE',
                'identifier="review_user_provided_configuration", '
                'security_class=SecurityClass.PASSIVE',
            ),
            (
                '"run_authorized_lab_scan",\n        SecurityClass.ACTIVE,\n'
                '        "authorized_lab_scan_v1",',
                'identifier="run_authorized_lab_scan",\n'
                '        security_class=SecurityClass.ACTIVE,\n'
                '        contract_type="authorized_lab_scan_v1",',
            ),
            (
                '"obtain_credentials", SecurityClass.PROHIBITED',
                'identifier="obtain_credentials", '
                'security_class=SecurityClass.PROHIBITED',
            ),
            (
                '"execute_generated_code", SecurityClass.PROHIBITED',
                'identifier="execute_generated_code", '
                'security_class=SecurityClass.PROHIBITED',
            ),
            (
                '"target_unauthorized_system", SecurityClass.PROHIBITED',
                'identifier="target_unauthorized_system", '
                'security_class=SecurityClass.PROHIBITED',
            ),
        )
        for old, new in replacements:
            source = source.replace(old, new)
        self._assert_passes(source, _DECISIONS_TEMPLATE, _POLICY_TEMPLATE)

    def test_extra_code_and_changed_security_class_are_rejected(self):
        cases = (
            _CAPABILITIES_TEMPLATE + '\nopen("outside.txt", "w")\n',
            _CAPABILITIES_TEMPLATE.replace(
                '"obtain_credentials", SecurityClass.PROHIBITED',
                '"obtain_credentials", SecurityClass.PASSIVE',
            ),
        )
        for source in cases:
            with self.subTest(source=source[-80:]):
                detail = self._assert_fails(
                    source, _DECISIONS_TEMPLATE, _POLICY_TEMPLATE
                )
                self.assertIn("capabilities module", detail)

    def test_changed_reason_code_and_policy_precedence_are_rejected(self):
        changed_decisions = _DECISIONS_TEMPLATE.replace(
            'CAPABILITY_PROHIBITED = "CAPABILITY_PROHIBITED"',
            'CAPABILITY_PROHIBITED = "CAPABILITY_ALLOWED"',
        )
        detail = self._assert_fails(
            _CAPABILITIES_TEMPLATE,
            changed_decisions,
            _POLICY_TEMPLATE,
        )
        self.assertIn("decisions module", detail)

        changed_policy = _POLICY_TEMPLATE.replace(
            "DecisionCode.REJECTED,\n"
            "                        ReasonCode.CAPABILITY_PROHIBITED,",
            "DecisionCode.APPROVED,\n"
            "                        ReasonCode.CAPABILITY_PROHIBITED,",
            1,
        )
        detail = self._assert_fails(
            _CAPABILITIES_TEMPLATE,
            _DECISIONS_TEMPLATE,
            changed_policy,
        )
        self.assertIn("policy module", detail)

    def test_task_spec_requires_all_advanced_suite_files(self):
        data = {
            "task_id": "advanced-policy",
            "objective": "Build an advanced policy",
            "allowed_files": [
                "agent/capabilities.py",
                "agent/decisions.py",
                "agent/policy.py",
            ],
            "checks": [
                {
                    "kind": "host_suite",
                    "suite": "advanced_defensive_policy_v1",
                }
            ],
        }
        spec = parse_task_spec(json.dumps(data))
        self.assertEqual(spec.checks[0].suite, "advanced_defensive_policy_v1")

        data["allowed_files"] = data["allowed_files"][:-1]
        with self.assertRaisesRegex(ValueError, "suite files"):
            parse_task_spec(json.dumps(data))

    def _assert_passes(self, capabilities, decisions, policy):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(
                directory, capabilities, decisions, policy
            )
            passed, detail = run_host_suite(
                "advanced_defensive_policy_v1", workspace
            )
        self.assertTrue(passed, detail)

    def _assert_fails(self, capabilities, decisions, policy):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(
                directory, capabilities, decisions, policy
            )
            passed, detail = run_host_suite(
                "advanced_defensive_policy_v1", workspace
            )
        self.assertFalse(passed)
        return detail

    @staticmethod
    def _workspace(directory, capabilities, decisions, policy):
        root = Path(directory)
        (root / "agent").mkdir()
        files = {
            "capabilities.py": capabilities,
            "decisions.py": decisions,
            "policy.py": policy,
        }
        for name, content in files.items():
            (root / "agent" / name).write_text(content, encoding="utf-8")
        return WorkspaceFileTools(
            root,
            SubagentTask(
                "advanced-policy",
                "validate advanced policy",
                tuple(f"agent/{name}" for name in files),
            ),
        )


if __name__ == "__main__":
    unittest.main()
