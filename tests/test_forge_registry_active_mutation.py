import unittest
from unittest.mock import patch

from src.forge_runtime import capability_registry_suite as suite
from src.forge_runtime.capability_registry_contract import EXPECTED_SOURCE
from tests import test_forge_capability_registry_suite as fixtures


class RegistryActiveMutationTests(unittest.TestCase):
    def test_each_suite_run_creates_unknown_name_with_actual_forge_enum_and_definition(self):
        observed = []
        load = suite._load_definitions
        def track(source, expected, name, dependencies):
            core = load(source, expected, name, dependencies)
            if name == 'forge_registry_policy':
                original = core.assess_request
                def assess(request, **kwargs):
                    names = request.get('capabilities', []) if type(request) is dict else []
                    if names and type(names[0]) is str and names[0].startswith('host_active_'):
                        definition = core.CAPABILITY_REGISTRY[names[0]]
                        self.assertIs(definition.security_class, core.SecurityClass.ACTIVE)
                        self.assertNotIn('requires_contract', definition.__dataclass_fields__)
                        self.assertNotIn(names[0], suite.CORE_SOURCE)
                        observed.append(names[0])
                    return original(request, **kwargs)
                core.assess_request = assess
            return core
        with patch.object(suite, '_load_definitions', side_effect=track):
            self.assertIsNone(suite.run_capability_registry(fixtures.CapabilityRegistryTests().contents()))
            self.assertIsNone(suite.run_capability_registry(fixtures.CapabilityRegistryTests().contents()))
        self.assertEqual(len(observed), 4)
        self.assertEqual(len(set(observed)), 2)

    def test_mutation_detects_name_allowlist_bug_and_restores_original_mapping(self):
        registry = suite._load_registry(EXPECTED_SOURCE)
        class BrokenCore:
            CAPABILITY_REGISTRY = registry.CAPABILITY_REGISTRY
            def assess_request(self, request, **kwargs):
                return {'decision': 'approved', 'reason_codes': [], 'reasons': []}
        core = BrokenCore()
        with self.assertRaisesRegex(AssertionError, 'bypassed mandatory host contract'):
            suite._new_active_capability_case(core, registry)
        self.assertIs(core.CAPABILITY_REGISTRY, registry.CAPABILITY_REGISTRY)


if __name__ == '__main__':
    unittest.main()
