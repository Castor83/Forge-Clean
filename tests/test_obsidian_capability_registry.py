import unittest
from types import MappingProxyType
from unittest.mock import patch

from src.obsidian_policy import (
    CAPABILITY_REGISTRY,
    CapabilityDefinition,
    SecurityClass,
)
from src.obsidian_policy.core import assess_request


class TestObsidianCapabilityRegistry(unittest.TestCase):
    def setUp(self):
        self.active_request = {
            "purpose": "Run a bounded check in my isolated local lab",
            "target_scope": "isolated_local_lab",
            "authorization_confirmed": True,
            "capabilities": ["run_authorized_lab_scan"],
        }

    def test_registry_and_entries_are_immutable(self):
        with self.assertRaises(TypeError):
            CAPABILITY_REGISTRY["new_capability"] = CapabilityDefinition(
                "new_capability", SecurityClass.PASSIVE
            )
        with self.assertRaises((AttributeError, TypeError)):
            CAPABILITY_REGISTRY["explain_security_concepts"].security_class = (
                SecurityClass.ACTIVE
            )

    def test_every_registered_capability_has_an_explicit_security_class(self):
        for identifier, definition in CAPABILITY_REGISTRY.items():
            with self.subTest(identifier=identifier):
                self.assertIsInstance(definition.security_class, SecurityClass)

    def test_unknown_security_class_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "known SecurityClass"):
            CapabilityDefinition("unsafe_metadata", "active")

    def test_registry_has_no_switch_that_can_disable_active_contracts(self):
        fields = CapabilityDefinition.__dataclass_fields__
        self.assertEqual(
            set(fields), {"identifier", "security_class", "contract_type"}
        )
        self.assertNotIn("requires_contract", fields)

    def test_active_capability_requires_a_contract_type(self):
        with self.assertRaisesRegex(ValueError, "must name a contract_type"):
            CapabilityDefinition("active_without_contract", SecurityClass.ACTIVE)

    def test_active_is_rejected_despite_authorization_and_allowed_scope(self):
        result = assess_request(self.active_request)
        self.assertEqual(result["decision"], "rejected")
        self.assertIn("validated execution contract", result["reasons"][0])

    def test_unvalidated_or_wrong_contract_cannot_enable_active_capability(self):
        for contract in (
            {"validated": False, "contract_type": "authorized_lab_scan_v1"},
            {"validated": True, "contract_type": "wrong_type"},
        ):
            with self.subTest(contract=contract):
                request = dict(self.active_request, execution_contract=contract)
                self.assertEqual(assess_request(request)["decision"], "rejected")

    def test_new_active_capability_is_rejected_without_name_in_gate_code(self):
        identifier = "new_active_test_capability"
        registry = MappingProxyType(
            {
                **CAPABILITY_REGISTRY,
                identifier: CapabilityDefinition(
                    identifier,
                    SecurityClass.ACTIVE,
                    contract_type="new_test_contract_v1",
                ),
            }
        )
        request = dict(self.active_request, capabilities=[identifier])

        with patch("src.obsidian_policy.core.CAPABILITY_REGISTRY", registry):
            result = assess_request(request)

        self.assertEqual(result["decision"], "rejected")
        self.assertIn(identifier, result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
