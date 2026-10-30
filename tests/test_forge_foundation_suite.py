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


CAPABILITIES_SOURCE = '''from dataclasses import dataclass
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
    "review_user_provided_configuration": CapabilityDefinition(
        "review_user_provided_configuration", SecurityClass.PASSIVE
    ),
    "obtain_credentials": CapabilityDefinition(
        "obtain_credentials", SecurityClass.PROHIBITED
    ),
    "execute_generated_code": CapabilityDefinition(
        "execute_generated_code", SecurityClass.PROHIBITED
    ),
})
'''

DECISIONS_SOURCE = '''from enum import Enum

class Decision(Enum):
    APPROVED = "approved"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"
'''

POLICY_SOURCE = '''from .capabilities import CAPABILITY_REGISTRY, SecurityClass
from .decisions import Decision

EXPECTED_FIELDS = frozenset({
    "purpose", "target_scope", "authorization_confirmed", "capability"
})
ALLOWED_TARGET_SCOPES = frozenset({
    "owned_system",
    "explicitly_authorized_system",
    "isolated_local_lab",
    "authorized_test_data",
})

def assess_request(request):
    if type(request) is not dict:
        return Decision.NEEDS_CLARIFICATION
    if set(request) != EXPECTED_FIELDS:
        return Decision.NEEDS_CLARIFICATION
    purpose = request.get("purpose")
    target_scope = request.get("target_scope")
    authorization_confirmed = request.get("authorization_confirmed")
    capability = request.get("capability")
    if type(purpose) is not str or not purpose.strip():
        return Decision.NEEDS_CLARIFICATION
    if target_scope not in ALLOWED_TARGET_SCOPES:
        return Decision.NEEDS_CLARIFICATION
    if authorization_confirmed is not True:
        return Decision.NEEDS_CLARIFICATION
    if type(capability) is not str or not capability.strip():
        return Decision.NEEDS_CLARIFICATION
    definition = CAPABILITY_REGISTRY.get(capability)
    if definition is None:
        return Decision.NEEDS_CLARIFICATION
    if definition.security_class is SecurityClass.PROHIBITED:
        return Decision.REJECTED
    return Decision.APPROVED
'''


class TestDefensivePolicyFoundationSuite(unittest.TestCase):
    def test_three_module_foundation_passes(self):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(
                directory,
                CAPABILITIES_SOURCE,
                DECISIONS_SOURCE,
                POLICY_SOURCE,
            )

            passed, detail = run_host_suite(
                "defensive_policy_foundation_v1", workspace
            )

        self.assertTrue(passed, detail)

    def test_exact_constructor_keywords_are_normalized(self):
        source = CAPABILITIES_SOURCE.replace(
            '"explain_security_concepts", SecurityClass.PASSIVE',
            'identifier="explain_security_concepts", '
            'security_class=SecurityClass.PASSIVE',
        ).replace(
            '"review_user_provided_configuration", SecurityClass.PASSIVE',
            'identifier="review_user_provided_configuration", '
            'security_class=SecurityClass.PASSIVE',
        ).replace(
            '"obtain_credentials", SecurityClass.PROHIBITED',
            'identifier="obtain_credentials", '
            'security_class=SecurityClass.PROHIBITED',
        ).replace(
            '"execute_generated_code", SecurityClass.PROHIBITED',
            'identifier="execute_generated_code", '
            'security_class=SecurityClass.PROHIBITED',
        )
        self._assert_passes(source, DECISIONS_SOURCE, POLICY_SOURCE)

    def test_extra_top_level_code_is_rejected_without_execution(self):
        source = CAPABILITIES_SOURCE + '\nopen("outside.txt", "w")\n'
        detail = self._assert_fails(source, DECISIONS_SOURCE, POLICY_SOURCE)
        self.assertIn("capabilities module", detail)

    def test_changed_decision_code_is_rejected(self):
        source = DECISIONS_SOURCE.replace('"rejected"', '"allowed"')
        detail = self._assert_fails(CAPABILITIES_SOURCE, source, POLICY_SOURCE)
        self.assertIn("decisions module", detail)

    def test_changed_policy_branch_is_rejected(self):
        source = POLICY_SOURCE.replace(
            "return Decision.REJECTED",
            "return Decision.APPROVED",
        )
        detail = self._assert_fails(CAPABILITIES_SOURCE, DECISIONS_SOURCE, source)
        self.assertIn("policy module", detail)

    def test_task_spec_binds_suite_to_all_three_files(self):
        data = {
            "task_id": "foundation",
            "objective": "Build a defensive policy foundation",
            "allowed_files": [
                "agent/capabilities.py",
                "agent/decisions.py",
                "agent/policy.py",
            ],
            "checks": [
                {
                    "kind": "host_suite",
                    "suite": "defensive_policy_foundation_v1",
                }
            ],
        }

        spec = parse_task_spec(json.dumps(data))

        self.assertEqual(spec.checks[0].suite, "defensive_policy_foundation_v1")
        data["allowed_files"].pop()
        with self.assertRaisesRegex(ValueError, "suite files"):
            parse_task_spec(json.dumps(data))

    def _assert_passes(self, capabilities, decisions, policy):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(
                directory, capabilities, decisions, policy
            )
            passed, detail = run_host_suite(
                "defensive_policy_foundation_v1", workspace
            )
        self.assertTrue(passed, detail)

    def _assert_fails(self, capabilities, decisions, policy):
        with TemporaryDirectory() as directory:
            workspace = self._workspace(
                directory, capabilities, decisions, policy
            )
            passed, detail = run_host_suite(
                "defensive_policy_foundation_v1", workspace
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
        task = SubagentTask(
            "foundation",
            "validate foundation",
            tuple(f"agent/{name}" for name in files),
        )
        return WorkspaceFileTools(root, task)


if __name__ == "__main__":
    unittest.main()
