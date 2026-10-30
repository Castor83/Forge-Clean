from dataclasses import fields, FrozenInstanceError
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.forge_runtime.capability_registry_contract import EXPECTED_SOURCE, REFERENCE_SHA256
from src.forge_runtime import capability_registry_suite as suite
from src.forge_runtime.audit_acl_contract import EXPECTED_SOURCE as ACL
from src.forge_runtime.durable_audit_contract import EXPECTED_SOURCE as AUDIT
from src.forge_runtime.audited_config_analysis_suite import _EXPECTED_SOURCE as FACADE
from src.forge_runtime.config_analysis_suite import _CONFIG_ANALYZER_TEMPLATE as ANALYZER
from src.forge_runtime.policy_service_contract import CORE_SOURCE, SERVICE_SOURCE
from src.forge_runtime.host_suites import HOST_SUITE_REGISTRY, run_host_suite
from src.forge_runtime.task_runner import parse_task_spec
from src.forge_runtime.workspace_tools import WorkspaceFileTools

ROOT = Path(__file__).resolve().parents[1]


class CapabilityRegistryTests(unittest.TestCase):
    def contents(self):
        return {'agent/capability_registry.py': EXPECTED_SOURCE, 'agent/core.py': CORE_SOURCE,
                'agent/decision_service.py': SERVICE_SOURCE, 'agent/audit.py': AUDIT,
                'agent/audit_acl.py': ACL, 'agent/config_analysis_service.py': FACADE,
                'agent/config_analyzer.py': ANALYZER}

    def test_complete_reference_integrates_all_seven_modules(self):
        self.assertIsNone(suite.run_capability_registry(self.contents()))

    def test_frozen_reference_and_spec_are_bound_before_model_build(self):
        raw = (ROOT / 'src/obsidian_policy/capability_registry.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), REFERENCE_SHA256)
        self.assertEqual(raw.decode('utf-8'), EXPECTED_SOURCE)
        spec = (ROOT / 'examples/forge_capability_registry_SPEC.md').read_text(encoding='utf-8')
        self.assertEqual(spec.split('```python\n', 1)[1].split('```', 1)[0], EXPECTED_SOURCE)
        task = parse_task_spec((ROOT / 'examples/forge_capability_registry.json').read_text())
        self.assertEqual(task.task.allowed_files, ('agent/capability_registry.py',))
        self.assertEqual(task.task.read_only_files, ('SPEC.md',) + suite.DEPENDENCIES)
        self.assertEqual(HOST_SUITE_REGISTRY['capability_registry_v1'].required_files, suite.REQUIRED_FILES)

    def test_registry_table_and_immutable_definitions(self):
        module = suite._load_registry(EXPECTED_SOURCE)
        registry, classes = module.CAPABILITY_REGISTRY, module.SecurityClass
        self.assertEqual(len(registry), 13)
        self.assertEqual([sum(d.security_class is c for d in registry.values()) for c in classes], [5, 1, 7])
        self.assertEqual([f.name for f in fields(module.CapabilityDefinition)], ['identifier', 'security_class', 'contract_type'])
        for name in suite.PASSIVE:
            self.assertIs(registry[name].security_class, classes.PASSIVE)
            self.assertIsNone(registry[name].contract_type)
        for name in suite.PROHIBITED:
            self.assertIs(registry[name].security_class, classes.PROHIBITED)
            self.assertIsNone(registry[name].contract_type)
        active = registry['run_authorized_lab_scan']
        self.assertIs(active.security_class, classes.ACTIVE)
        self.assertEqual(active.contract_type, 'authorized_lab_scan_v1')
        self.assertFalse(hasattr(active, '__dict__'))
        with self.assertRaises(TypeError):
            registry['extra'] = active
        with self.assertRaises(TypeError):
            registry[active.identifier] = active
        with self.assertRaises(TypeError):
            del registry[active.identifier]
        with self.assertRaises(FrozenInstanceError):
            active.contract_type = None

    def test_invalid_definitions_and_duplicates_rejected(self):
        module = suite._load_registry(EXPECTED_SOURCE)
        cls, definition = module.SecurityClass, module.CapabilityDefinition
        cases = [('', cls.PASSIVE, None), ('  ', cls.PASSIVE, None), (None, cls.PASSIVE, None),
                 (True, cls.PASSIVE, None), ('x', 'active', 'c'), ('x', object(), None)]
        cases += [('x', cls.ACTIVE, v) for v in (None, '', '  ', 1, True)]
        cases += [('x', c, v) for c in (cls.PASSIVE, cls.PROHIBITED) for v in ('c', '', 1)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                definition(*args)
        valid = definition('lab', cls.ACTIVE, 'contract')
        self.assertEqual(valid.identifier, 'lab')
        with self.assertRaises(ValueError):
            module._build_registry((valid, valid))

    def test_adversarial_sources_stop_before_loading_or_native_integration(self):
        sources = [
            EXPECTED_SOURCE.replace('SecurityClass.PROHIBITED)', 'SecurityClass.PASSIVE)', 1),
            EXPECTED_SOURCE.replace('frozen=True', 'frozen=False'),
            EXPECTED_SOURCE.replace('not self.identifier.strip()', 'False'),
            EXPECTED_SOURCE.replace('return MappingProxyType(registry)', 'return registry'),
            EXPECTED_SOURCE.replace('CapabilityDefinition("execute_generated_code", SecurityClass.PROHIBITED),',
                                    'CapabilityDefinition("extra", SecurityClass.PASSIVE),'),
            EXPECTED_SOURCE + '\nopen("bad", "w").write("bad")\n',
            EXPECTED_SOURCE + '\nimport subprocess\n',
        ]
        for source in sources:
            with self.subTest(source=source[-80:]), patch.object(suite, '_load_registry') as load, patch.object(suite, '_run_integration') as native:
                contents = self.contents()
                contents['agent/capability_registry.py'] = source
                self.assertIn('exact frozen AST', suite.run_capability_registry(contents))
                load.assert_not_called()
                native.assert_not_called()

    def test_loader_itself_rejects_mutation_and_never_executes_import_statements(self):
        with self.assertRaises(ValueError):
            suite._load_registry(EXPECTED_SOURCE + '\nimport os\n')
        module = suite._load_registry(EXPECTED_SOURCE)
        self.assertNotIn('__import__', module.__dict__['__builtins__'])
        self.assertNotIn('os', module.__dict__)

    def test_dependency_mutations_still_rejected(self):
        for name in suite.DEPENDENCIES:
            with self.subTest(name=name):
                contents = self.contents()
                contents[name] += '\nprint("unexpected")\n'
                self.assertIsNotNone(suite.run_capability_registry(contents))

    def test_missing_required_files_fail_through_real_host_suite(self):
        spec = parse_task_spec((ROOT / 'examples/forge_capability_registry.json').read_text())
        with TemporaryDirectory(dir=ROOT) as directory:
            workspace = WorkspaceFileTools(Path(directory), spec.task)
            passed, _ = run_host_suite('capability_registry_v1', workspace)
            self.assertFalse(passed)

    def test_policy_receives_actual_forge_mapping_and_enum(self):
        module = suite._load_registry(EXPECTED_SOURCE)
        original = suite._load_definitions
        observed = []
        def tracked(source, expected, name, dependencies):
            if name == 'forge_registry_policy':
                self.assertIs(dependencies['CAPABILITY_REGISTRY'], module.CAPABILITY_REGISTRY)
                self.assertIs(dependencies['SecurityClass'], module.SecurityClass)
                observed.append(name)
            return original(source, expected, name, dependencies)
        with patch.object(suite, '_load_definitions', side_effect=tracked):
            suite._run_integration(self.contents(), module)
        self.assertEqual(observed, ['forge_registry_policy'])

    def test_registry_specific_policy_cases_exercise_incomplete_and_wrong_contract(self):
        observed = []
        original = suite._load_definitions
        def tracked(source, expected, name, dependencies):
            result = original(source, expected, name, dependencies)
            if name == 'forge_registry_policy':
                assess = result.assess_request
                def record(request, **kwargs):
                    observed.append((request, kwargs.get('validated_contract')))
                    return assess(request, **kwargs)
                result.assess_request = record
            return result
        with patch.object(suite, '_load_definitions', side_effect=tracked):
            suite._run_integration(self.contents(), suite._load_registry(EXPECTED_SOURCE))
        self.assertTrue(all(any(request == {'capabilities': [name]} for request, _ in observed)
                            for name in suite.PROHIBITED))
        self.assertTrue(any(getattr(contract, 'contract_type', None) == 'different_contract_v1'
                            for _, contract in observed))


if __name__ == '__main__':
    unittest.main()
