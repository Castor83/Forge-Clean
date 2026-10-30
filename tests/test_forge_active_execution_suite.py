"""Admission and behavior mutation proofs for the fake-socket active suite."""

import ast
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from src.forge_runtime import active_execution_suite as suite
from src.forge_runtime.active_execution_contract import EXPECTED_SOURCE, REFERENCE_SHA256
from src.forge_runtime.execution_contract_contract import EXPECTED_SOURCE as VALIDATOR_SOURCE

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED = ROOT/'workspaces/forge-registry-reviewer-repeat-20261003/run-1'


def accepted_contents():
    return {
        'agent/active_scan.py': EXPECTED_SOURCE,
        'agent/execution_contract.py': VALIDATOR_SOURCE,
        **{name: (ACCEPTED/name).read_text(encoding='utf-8') for name in suite.validator_suite.DEPENDENCIES},
    }


class ActiveExecutionSuiteTests(unittest.TestCase):
    def test_reference_is_hardened_original_and_nine_full_gates(self):
        raw = (ROOT/'src/obsidian_policy/active_scan.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), REFERENCE_SHA256)
        self.assertEqual(EXPECTED_SOURCE, raw.decode('utf-8'))
        self.assertEqual(len(suite.REQUIRED_FILES), 9)
        self.assertEqual(len(suite.DEPENDENCIES), 8)
        trees = suite._validate_all_sources(accepted_contents())
        self.assertEqual(set(trees), set(suite.REQUIRED_FILES))

    def test_nine_real_forge_artifacts_pass_without_any_real_socket(self):
        with patch('socket.socket') as real_socket:
            self.assertIsNone(suite.run_active_execution(accepted_contents()))
        real_socket.assert_not_called()

    def test_every_source_ast_mutation_stops_loaders_integration_and_socket(self):
        for name in suite.REQUIRED_FILES:
            with self.subTest(name=name):
                contents = accepted_contents(); contents[name] += '\nforbidden_extra = True\n'
                with (patch.object(suite, '_load_active') as active_loader,
                      patch.object(suite.validator_suite, '_load_validator') as validator_loader,
                      patch.object(suite.validator_suite, '_run_integration') as integration,
                      patch.object(suite.validator_suite.registry_suite, '_load_registry') as registry_loader,
                      patch.object(suite.validator_suite, '_load_validated_acl') as acl_loader,
                      patch.object(suite.validator_suite, '_load_validated_storage') as storage_loader,
                      patch.object(suite.validator_suite, '_load_validated_analysis') as analysis_loader,
                      patch('socket.socket') as real_socket):
                    self.assertIsNotNone(suite.run_active_execution(contents))
                for operation in (active_loader, validator_loader, integration, registry_loader,
                                  acl_loader, storage_loader, analysis_loader, real_socket):
                    operation.assert_not_called()

    def test_missing_and_syntax_error_in_each_file_stop_before_loading(self):
        for name in suite.REQUIRED_FILES:
            for mode in ('missing', 'syntax'):
                with self.subTest(name=name, mode=mode):
                    contents = accepted_contents()
                    if mode == 'missing': contents.pop(name)
                    else: contents[name] = 'invalid syntax !'
                    with (patch.object(suite, '_load_active') as active_loader,
                          patch.object(suite.validator_suite, '_load_validator') as validator_loader,
                          patch.object(suite.validator_suite, '_run_integration') as integration,
                          patch('socket.socket') as real_socket):
                        self.assertIsNotNone(suite.run_active_execution(contents))
                    for operation in (active_loader, validator_loader, integration, real_socket):
                        operation.assert_not_called()

    def test_import_consumption_loopback_and_budget_source_mutations_are_gated(self):
        mutations = (
            EXPECTED_SOURCE+'\nimport subprocess\n',
            EXPECTED_SOURCE+'\nfrom socket import socket as direct_socket\n',
            EXPECTED_SOURCE.replace('address, port, transport = authorization.consume()',
                                    'address, port, transport = authorization.address, authorization.port, authorization.transport'),
            EXPECTED_SOURCE.replace('if not parsed.is_loopback or transport != "tcp":', 'if False:'),
            EXPECTED_SOURCE.replace('if self._attempts >= self._contract.limits.max_connections:', 'if False:'),
        )
        for source in mutations:
            with self.subTest(source=source[-100:]):
                self.assertNotEqual(ast.dump(ast.parse(source)), suite._EXPECTED_AST)
                contents = accepted_contents(); contents['agent/active_scan.py'] = source
                with (patch.object(suite, '_load_active') as loader,
                      patch.object(suite.validator_suite, '_load_validator') as validator_loader,
                      patch.object(suite.validator_suite, '_run_integration') as integration,
                      patch('socket.socket') as real_socket):
                    self.assertIsNotNone(suite.run_active_execution(contents))
                for operation in (loader, validator_loader, integration, real_socket):
                    operation.assert_not_called()

    def test_actual_active_core_and_service_share_forge_validator_not_host_type(self):
        original = suite._load_active
        observed = {}
        def observe(source, validator, service):
            active = original(source, validator, service)
            observed.update(active=active, validator=validator, service=service)
            return active
        with patch.object(suite, '_load_active', observe):
            self.assertIsNone(suite.run_active_execution(accepted_contents()))
        active, validator, service = observed['active'], observed['validator'], observed['service']
        self.assertIs(active.ValidatedExecutionContract, validator.ValidatedExecutionContract)
        self.assertIs(service.ValidatedExecutionContract, validator.ValidatedExecutionContract)
        self.assertIs(active.assess_and_record_request, service.assess_and_record_request)
        self.assertIs(active.contract_is_current, validator.contract_is_current)
        from src.obsidian_policy.execution_contract import ValidatedExecutionContract
        self.assertIsNot(active.ValidatedExecutionContract, ValidatedExecutionContract)
        self.assertIsInstance(active.socket, suite._FakeSocket)
        with self.assertRaises(ImportError):
            active.__dict__['__builtins__']['__import__']('socket')

    def test_separate_missing_and_raw_forged_mutations_both_execute_on_real_active_path(self):
        with (patch.object(suite, '_mutation_missing_contract', wraps=suite._mutation_missing_contract) as missing,
              patch.object(suite, '_mutation_raw_forged_claim', wraps=suite._mutation_raw_forged_claim) as raw):
            self.assertIsNone(suite.run_active_execution(accepted_contents()))
        missing.assert_called_once(); raw.assert_called_once()
        self.assertIs(missing.call_args.args[0], raw.call_args.args[0])

    def test_both_genuine_pending_recovery_paths_resume_audited_active_scans(self):
        original = suite._active_recovery_cases
        observed = []
        def observe(active, validator, storage, directory):
            proofs = original(active, validator, storage, directory)
            observed.extend(proofs)
            return proofs
        with patch.object(suite, '_active_recovery_cases', observe), patch('socket.socket') as real_socket:
            self.assertIsNone(suite.run_active_execution(accepted_contents()))
        self.assertEqual(observed, [
            {'recovery_status': 'entry_absent', 'action': 'append_missing',
             'resumed_socket_contacts': 2, 'audit_entries': 2},
            {'recovery_status': 'entry_present', 'action': 'acknowledge_present',
             'resumed_socket_contacts': 2, 'audit_entries': 2},
        ])
        real_socket.assert_not_called()

    def test_behavior_matrix_rejects_claimed_recovery_success_without_new_execution(self):
        def change(active):
            original = active.run_authorized_lab_scan
            def claimed(request, contract, *, audit_path, **kwargs):
                if audit_path.parent.name.startswith('active-recovery-') and audit_path.is_file() and not audit_path.with_name('audit.jsonl.pending').exists():
                    # The first call still traverses the real audit-failure path;
                    # after recovery this mutation only claims execution success.
                    from types import SimpleNamespace
                    return SimpleNamespace(completed=True, status_code='EXECUTION_COMPLETED', attempts=2)
                return original(request, contract, audit_path=audit_path, **kwargs)
            active.run_authorized_lab_scan = claimed
        self.assertIn('recovered active scan did not actually complete', self._runtime_mutation(change))

    def _runtime_mutation(self, change):
        original = suite._load_active
        def changed(source, validator, service):
            module = original(source, validator, service)
            change(module)
            return module
        with patch.object(suite, '_load_active', changed), patch('socket.socket') as real_socket:
            failure = suite.run_active_execution(accepted_contents())
        self.assertIsNotNone(failure)
        real_socket.assert_not_called()
        return failure

    def test_behavior_matrix_detects_removal_of_one_time_consumption(self):
        def change(active):
            active._AuthorizedProbe.consume = lambda self: (self.address, self.port, self.transport)
        self.assertIn('forged/copied/replayed', self._runtime_mutation(change))

    def test_behavior_matrix_detects_removal_of_helper_own_loopback_tcp_check(self):
        def change(active):
            def unsafe(authorization, *, timeout_seconds):
                if type(authorization) is not active._AuthorizedProbe:
                    raise TypeError('fake')
                address, port, _transport = authorization.consume()
                with active.socket.socket(active.socket.AF_INET, active.socket.SOCK_STREAM) as connection:
                    connection.settimeout(timeout_seconds)
                    return connection.connect_ex((address, port)) == 0
            active._tcp_connect_probe = unsafe
        self.assertIn('helper allowed nonloopback/nonTCP', self._runtime_mutation(change))

    def test_behavior_matrix_detects_connection_budget_bypass(self):
        def change(active):
            original = active.ExecutionGuard.reserve_probe
            def unsafe(self, **kwargs):
                if self._attempts >= self._contract.limits.max_connections:
                    self._attempts = 0
                return original(self, **kwargs)
            active.ExecutionGuard.reserve_probe = unsafe
        self.assertIn('connection budget', self._runtime_mutation(change))

    def test_behavior_matrix_detects_imitated_guard_minting_bypass(self):
        self.assertIn('imitated guard minted', self._runtime_mutation(
            lambda active: setattr(active, '_require_issued_guard', lambda _guard: None)))

    def test_fake_socket_exposes_no_dns_or_payload_api(self):
        fake = suite._FakeSocket()
        connection = fake.socket(fake.AF_INET, fake.SOCK_STREAM)
        for name in ('getaddrinfo', 'gethostbyname', 'send', 'sendall'):
            self.assertFalse(hasattr(fake, name))
            self.assertFalse(hasattr(connection, name))


if __name__ == '__main__':
    unittest.main()
