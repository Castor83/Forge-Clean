"""Behavioural acceptance for eight real Forge artifacts and fail-closed gates."""

import ast
from dataclasses import fields
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from src.forge_runtime import execution_contract_suite as suite
from src.forge_runtime.execution_contract_contract import EXPECTED_SOURCE, REFERENCE_SHA256

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED = ROOT/'workspaces/forge-registry-reviewer-repeat-20261003/run-1'


def accepted_contents():
    return {'agent/execution_contract.py': EXPECTED_SOURCE,
            **{name: (ACCEPTED/name).read_text(encoding='utf-8') for name in suite.DEPENDENCIES}}


class ExecutionContractSuiteTests(unittest.TestCase):
    def test_reference_is_hardened_original_and_seven_fixed_accepted_modules(self):
        raw = (ROOT/'src/obsidian_policy/execution_contract.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), REFERENCE_SHA256)
        self.assertEqual(EXPECTED_SOURCE, raw.decode('utf-8'))
        self.assertEqual(len(suite.REQUIRED_FILES), 8)
        self.assertEqual(set(suite.DEPENDENCY_AST_SHA256), set(suite.DEPENDENCIES))
        suite._validate_all_sources(accepted_contents())

    def test_eight_actual_modules_pass_native_validator_and_passive_integration(self):
        self.assertIsNone(suite.run_execution_contract(accepted_contents()))

    def test_each_dependency_ast_mutation_stops_every_loader_and_integration(self):
        for name in suite.REQUIRED_FILES:
            with self.subTest(name=name):
                contents = accepted_contents()
                contents[name] += '\nforbidden_extra = True\n'
                with (patch.object(suite, '_load_validator') as loader, patch.object(suite, '_run_integration') as integration,
                     patch.object(suite.registry_suite, '_load_registry') as registry_loader,
                     patch.object(suite, '_load_validated_acl') as acl_loader,
                     patch.object(suite, '_load_validated_storage') as storage_loader,
                     patch.object(suite, '_load_validated_analysis') as analysis_loader):
                    self.assertIsNotNone(suite.run_execution_contract(contents))
                for blocked in (loader, integration, registry_loader, acl_loader, storage_loader, analysis_loader):
                    blocked.assert_not_called()

    def test_missing_or_invalid_python_stops_before_loader(self):
        for name in suite.REQUIRED_FILES:
            for mode in ('missing', 'syntax'):
                with self.subTest(name=name, mode=mode):
                    contents = accepted_contents()
                    if mode == 'missing': contents.pop(name)
                    else: contents[name] = 'invalid syntax !'
                    with patch.object(suite, '_load_validator') as loader, patch.object(suite, '_run_integration') as integration:
                        self.assertIsNotNone(suite.run_execution_contract(contents))
                    loader.assert_not_called(); integration.assert_not_called()

    def test_validator_loader_removes_generated_imports_and_preserves_future_annotations(self):
        validator = suite._load_validator(EXPECTED_SOURCE)
        self.assertIsInstance(validator.validate_execution_contract.__annotations__['return'], str)
        self.assertNotIn('__import__', validator.__dict__)
        self.assertNotIn('socket', validator.__dict__)
        contract = validator.validate_execution_contract(suite._raw_contract(), bound_request=suite._request(), now_utc=suite.NOW)
        self.assertTrue(validator.request_matches_contract(suite._request(), contract))
        self.assertTrue(validator.contract_is_current(contract, now_utc=suite.NOW))
        with self.assertRaises(ImportError): validator.__dict__['__builtins__']['__import__']('socket')

    def test_validator_adversarial_schema_target_resource_and_time_matrix(self):
        validator = suite._load_validator(EXPECTED_SOURCE)
        suite._validator_cases(validator)

    def test_real_core_service_use_actual_forge_validator_objects(self):
        original = suite._load_definitions
        bound = {}
        def observed(source, expected, name, dependencies):
            module = original(source, expected, name, dependencies)
            bound[name] = module
            return module
        with patch.object(suite, '_load_definitions', observed), patch.object(suite, '_load_validator', wraps=suite._load_validator) as loader:
            self.assertIsNone(suite.run_execution_contract(accepted_contents()))
        core = bound['forge_validator_policy']; service = bound['forge_validator_service']
        self.assertIs(core.ValidatedExecutionContract, service.ValidatedExecutionContract)
        self.assertIs(service._assess_request, core.assess_request)
        from src.obsidian_policy.execution_contract import ValidatedExecutionContract
        self.assertIsNot(core.ValidatedExecutionContract, ValidatedExecutionContract)
        self.assertEqual(loader.call_count, 1)

    def _reject_policy_bypass(self, kind):
        original = suite._load_definitions
        def bypass(source, expected, name, dependencies):
            module = original(source, expected, name, dependencies)
            if name == 'forge_validator_policy':
                assess = module.assess_request
                def changed(request, **kwargs):
                    active = isinstance(request, dict) and request.get('capabilities') == ['run_authorized_lab_scan']
                    supplied = kwargs.get('validated_contract')
                    if active and ((kind == 'missing_contract' and supplied is None) or
                                   (kind == 'raw_forged_claim' and type(supplied) is dict) or
                                   (kind == 'same_type_forged_claim' and type(supplied) is module.ValidatedExecutionContract
                                    and not module.request_matches_contract(request, supplied))):
                        return {'decision': 'approved', 'reason_codes': ['REQUEST_APPROVED'], 'reasons': ['bypass']}
                    return assess(request, **kwargs)
                module.assess_request = changed
            return module
        with patch.object(suite, '_load_definitions', bypass):
            failure = suite.run_execution_contract(accepted_contents())
        self.assertIsNotNone(failure)
        self.assertIn('raw_forged_claim' if kind == 'same_type_forged_claim' else kind, failure)

    def test_suite_detects_missing_contract_bypass_independently(self):
        self._reject_policy_bypass('missing_contract')

    def test_suite_detects_raw_forged_claim_bypass_independently(self):
        self._reject_policy_bypass('raw_forged_claim')

    def test_suite_detects_same_type_forged_contract_bypass_in_real_policy(self):
        self._reject_policy_bypass('same_type_forged_claim')

    def test_both_separate_named_mutations_run_on_real_core(self):
        with (patch.object(suite, '_mutation_missing_contract', wraps=suite._mutation_missing_contract) as missing,
             patch.object(suite, '_mutation_raw_forged_claim', wraps=suite._mutation_raw_forged_claim) as raw,
             patch.object(suite.registry_suite, '_new_active_capability_case', wraps=suite.registry_suite._new_active_capability_case) as unknown):
            self.assertIsNone(suite.run_execution_contract(accepted_contents()))
        missing.assert_called_once(); raw.assert_called_once(); unknown.assert_called_once()
        self.assertIs(missing.call_args.args[0], raw.call_args.args[0])
        self.assertIs(missing.call_args.args[0], unknown.call_args.args[0])

    def test_copied_actual_seal_and_same_type_forgery_do_not_gain_issuer_identity(self):
        validator = suite._load_validator(EXPECTED_SOURCE)
        request = suite._request()
        genuine = validator.validate_execution_contract(suite._raw_contract(), bound_request=request, now_utc=suite.NOW)
        forged = object.__new__(validator.ValidatedExecutionContract)
        for f in fields(genuine): object.__setattr__(forged, f.name, getattr(genuine, f.name))
        self.assertIs(type(genuine), type(forged))
        self.assertFalse(validator.request_matches_contract(request, forged))
        self.assertFalse(validator.contract_is_current(forged, now_utc=suite.NOW))
        self.assertTrue(validator.request_matches_contract(request, genuine))
        self.assertTrue(validator.contract_is_current(genuine, now_utc=suite.NOW))

    def test_source_mutations_to_issuer_binding_and_additional_import_are_gated(self):
        mutations = (EXPECTED_SOURCE+'\nimport socket\n', EXPECTED_SOURCE+'\n_VALIDATION_SEAL = object()\n',
                     EXPECTED_SOURCE.replace('return False', 'return True', 1))
        for source in mutations:
            self.assertNotEqual(ast.dump(ast.parse(source)), ast.dump(ast.parse(EXPECTED_SOURCE)))
            contents = accepted_contents(); contents['agent/execution_contract.py'] = source
            with patch.object(suite, '_load_validator') as loader, patch.object(suite, '_run_integration') as integration:
                self.assertIsNotNone(suite.run_execution_contract(contents))
            loader.assert_not_called(); integration.assert_not_called()


if __name__ == '__main__': unittest.main()
