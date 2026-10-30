import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime import (
    HOST_SUITE_REGISTRY,
    HostSuiteCheck,
    SubagentTask,
    WorkspaceFileTools,
    parse_task_spec,
    run_host_suite,
)
from src.forge_runtime.task_runner import run_forge_task


REGISTRY_SOURCE = '''from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class SecurityClass(Enum):
    PASSIVE = "passive"
    PROHIBITED = "prohibited"


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    identifier: str
    security_class: SecurityClass


CAPABILITY_REGISTRY = MappingProxyType({
    "explain_security_concepts": CapabilityDefinition(
        "explain_security_concepts", SecurityClass.PASSIVE
    ),
    "obtain_credentials": CapabilityDefinition(
        "obtain_credentials", SecurityClass.PROHIBITED
    ),
})
'''

POLICY_SOURCE = '''from .registry import CAPABILITY_REGISTRY, SecurityClass


def assess_capability(capability):
    definition = CAPABILITY_REGISTRY.get(capability)
    if definition is None:
        return "needs_clarification"
    if definition.security_class is SecurityClass.PROHIBITED:
        return "rejected"
    return "approved"
'''


def suite_json(suite="two_module_policy_v1", allowed_files=None):
    return json.dumps(
        {
            "task_id": "two-module-policy",
            "objective": "Build a fixed two-module policy agent",
            "allowed_files": allowed_files
            or ["agent/registry.py", "agent/policy.py"],
            "checks": [{"kind": "host_suite", "suite": suite}],
        }
    )


class TestHostSuiteRegistry(unittest.TestCase):
    def test_registry_is_immutable_and_exact(self):
        self.assertEqual(
            set(HOST_SUITE_REGISTRY),
            {
                "two_module_policy_v1",
                "policy_service_v1",
                "capability_registry_v1",
                "execution_contract_v1",
                "active_execution_v1",
                "f001_file_analyzer_v1",
                "defensive_policy_foundation_v1",
                "advanced_defensive_policy_v1",
                "redacted_config_analysis_v1",
                "minimized_audit_chain_v1",
                "jsonl_audit_log_v1",
                "audited_decision_gate_v1",
                "audited_config_analysis_v1",
                "audit_recovery_v1",
                "audit_acl_v1",
                "durable_audit_v1",
                "partitioned_audit_acl_v1",
                "partitioned_durable_audit_v1",
            },
        )
        definition = HOST_SUITE_REGISTRY["two_module_policy_v1"]
        self.assertEqual(
            definition.required_files,
            ("agent/registry.py", "agent/policy.py"),
        )
        with self.assertRaises(TypeError):
            HOST_SUITE_REGISTRY["other"] = definition

    def test_complete_two_module_policy_passes(self):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(directory, REGISTRY_SOURCE, POLICY_SOURCE)

            passed, detail = run_host_suite("two_module_policy_v1", workspace)

        self.assertTrue(passed, detail)
        self.assertEqual(detail, "host suite two_module_policy_v1 passed")

    def test_exact_constructor_keywords_are_also_accepted(self):
        source = REGISTRY_SOURCE.replace(
            '"explain_security_concepts", SecurityClass.PASSIVE',
            'identifier="explain_security_concepts", '
            'security_class=SecurityClass.PASSIVE',
        ).replace(
            '"obtain_credentials", SecurityClass.PROHIBITED',
            'identifier="obtain_credentials", '
            'security_class=SecurityClass.PROHIBITED',
        )
        with TemporaryDirectory() as directory:
            workspace = self._workspace(directory, source, POLICY_SOURCE)

            passed, detail = run_host_suite("two_module_policy_v1", workspace)

        self.assertTrue(passed, detail)

    def test_rejects_mutable_registry(self):
        mutable = REGISTRY_SOURCE.replace(
            "CAPABILITY_REGISTRY = MappingProxyType({",
            "CAPABILITY_REGISTRY = {",
        ).replace("\n})\n", "\n}\n")
        self._assert_suite_fails(mutable, POLICY_SOURCE, "MappingProxyType")

    def test_rejects_registry_top_level_code(self):
        source = REGISTRY_SOURCE + '\nopen("outside.txt", "w")\n'
        self._assert_suite_fails(source, POLICY_SOURCE, "contain only")

    def test_rejects_policy_with_unsafe_import(self):
        source = "import os\n" + POLICY_SOURCE
        self._assert_suite_fails(REGISTRY_SOURCE, source, "contain only")

    def test_rejects_wrong_unknown_behavior(self):
        source = POLICY_SOURCE.replace(
            'return "needs_clarification"', 'return "approved"'
        )
        self._assert_suite_fails(REGISTRY_SOURCE, source, "safe structure")

    def test_rejects_dunder_attribute_escape(self):
        source = POLICY_SOURCE.replace(
            "definition = CAPABILITY_REGISTRY.get(capability)",
            "definition = capability.__class__",
        )
        self._assert_suite_fails(REGISTRY_SOURCE, source, "safe structure")

    def test_registered_suite_parses_as_host_check(self):
        spec = parse_task_spec(suite_json())

        self.assertEqual(
            spec.checks,
            (HostSuiteCheck("host_suite", "two_module_policy_v1"),),
        )

    def test_unknown_suite_and_incomplete_coverage_are_rejected(self):
        cases = (
            suite_json("unknown_suite"),
            suite_json(allowed_files=["agent/registry.py"]),
            suite_json(
                allowed_files=[
                    "agent/registry.py",
                    "agent/policy.py",
                    "agent/extra.py",
                ]
            ),
        )
        for content in cases:
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    parse_task_spec(content)

    def test_model_success_claim_cannot_override_suite_failure(self):
        class ScriptedChat:
            def __init__(self, responses):
                self.responses = iter(responses)

            def __call__(self, _messages):
                return next(self.responses)

        invalid_policy = POLICY_SOURCE.replace(
            'return "needs_clarification"', 'return "approved"'
        )
        responses = (
            json.dumps(
                {
                    "action": "write_file",
                    "path": "agent/registry.py",
                    "content": REGISTRY_SOURCE,
                }
            ),
            json.dumps(
                {
                    "action": "write_file",
                    "path": "agent/policy.py",
                    "content": invalid_policy,
                }
            ),
            json.dumps(
                {
                    "action": "finish",
                    "passed": True,
                    "summary": "built",
                    "findings": [],
                }
            ),
            json.dumps({"action": "run_check"}),
            json.dumps(
                {
                    "action": "finish",
                    "passed": True,
                    "summary": "I claim the suite passed",
                    "findings": [],
                }
            ),
        )
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "successful host check"):
                run_forge_task(
                    Path(directory),
                    parse_task_spec(suite_json()),
                    ScriptedChat(responses),
                )

    def _assert_suite_fails(self, registry, policy, expected_detail):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(directory, registry, policy)

            passed, detail = run_host_suite("two_module_policy_v1", workspace)

        self.assertFalse(passed)
        self.assertIn(expected_detail, detail)

    @staticmethod
    def _workspace(directory, registry, policy):
        root = Path(directory)
        (root / "agent").mkdir()
        (root / "agent" / "registry.py").write_text(registry, encoding="utf-8")
        (root / "agent" / "policy.py").write_text(policy, encoding="utf-8")
        task = SubagentTask(
            "host-suite",
            "validate policy",
            ("agent/registry.py", "agent/policy.py"),
        )
        return WorkspaceFileTools(root, task)


if __name__ == "__main__":
    unittest.main()
