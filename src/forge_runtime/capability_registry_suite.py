"""Exact registry gate followed by fixed seven-module native acceptance."""

import ast
from dataclasses import dataclass, fields, FrozenInstanceError, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Mapping
from uuid import uuid4

from src.obsidian_policy.execution_contract import (
    ValidatedExecutionContract, contract_is_current, request_matches_contract,
    validate_execution_contract, ContractValidationError,
)
from .capability_registry_contract import EXPECTED_SOURCE
from .policy_service_suite import (
    CORE_SOURCE, SERVICE_SOURCE, _load_definitions, _policy_cases,
    _service_order_cases, _require, NOW,
)
from .audit_acl_suite import _load_validated_acl
from .audited_config_analysis_suite import _EXPECTED_TREE as FACADE_TREE
from .config_analysis_suite import _load_validated_analysis, run_redacted_config_analysis
from .durable_audit_suite import _load_validated_storage, run_durable_audit
from .passive_integration_verifier import _run_passive_cases
from .recovery_suite import _run_storage_cases

DEPENDENCIES = (
    'agent/core.py', 'agent/decision_service.py', 'agent/audit.py',
    'agent/audit_acl.py', 'agent/config_analysis_service.py', 'agent/config_analyzer.py',
)
REQUIRED_FILES = ('agent/capability_registry.py',) + DEPENDENCIES
PASSIVE = ('explain_security_concepts', 'review_user_provided_configuration',
           'analyze_user_provided_logs', 'assess_local_lab_fixture', 'create_defensive_checklist')
PROHIBITED = ('establish_persistence', 'evade_detection', 'obtain_credentials',
              'exfiltrate_data', 'disrupt_system', 'target_unauthorized_system', 'execute_generated_code')
_EXPECTED_AST = ast.dump(ast.parse(EXPECTED_SOURCE))


def _validate_registry(source):
    tree = ast.parse(source)
    if ast.dump(tree) != _EXPECTED_AST:
        raise ValueError('registry must match its exact frozen AST')
    return tree


def _load_registry(source):
    # Defense in depth: the loader itself always repeats the complete gate.
    tree = _validate_registry(source)
    module = ModuleType('forge_validated_capability_registry')
    module.__dict__.update({
        'dataclass': dataclass, 'Enum': Enum, 'MappingProxyType': MappingProxyType,
        'Mapping': Mapping,
        '__builtins__': {'__build_class__': __build_class__, 'type': type,
                         'isinstance': isinstance, 'str': str, 'tuple': tuple,
                         'dict': dict, 'ValueError': ValueError},
    })
    body = [n for n in tree.body if type(n) in (ast.Assign, ast.ClassDef, ast.FunctionDef)]
    definitions = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    # Generated import statements never run; every dependency is host-injected.
    exec(compile(definitions, '<forge-registry-suite>', 'exec'), module.__dict__)
    return module


def _registry_cases(module):
    classes, definition, registry = module.SecurityClass, module.CapabilityDefinition, module.CAPABILITY_REGISTRY
    _require([(c.name, c.value) for c in classes] ==
             [('PASSIVE', 'passive'), ('ACTIVE', 'active'), ('PROHIBITED', 'prohibited')], 'security classes changed')
    _require(tuple(registry) == PASSIVE + ('run_authorized_lab_scan',) + PROHIBITED, 'registry entries changed')
    _require(tuple(f.name for f in fields(definition)) ==
             ('identifier', 'security_class', 'contract_type'), 'definition fields changed')
    for name in PASSIVE + PROHIBITED:
        expected = classes.PASSIVE if name in PASSIVE else classes.PROHIBITED
        _require(registry[name].security_class is expected and registry[name].contract_type is None,
                 'passive/prohibited metadata changed')
    active = registry['run_authorized_lab_scan']
    _require(active.security_class is classes.ACTIVE and active.contract_type == 'authorized_lab_scan_v1',
             'active contract changed')
    _require(not hasattr(active, '__dict__'), 'slots removed')
    for operation in (
        lambda: registry.__setitem__('new', active), lambda: registry.__setitem__(active.identifier, active),
        lambda: registry.__delitem__(active.identifier), lambda: setattr(active, 'contract_type', None),
    ):
        try:
            operation()
        except (AttributeError, TypeError, FrozenInstanceError):
            pass
        else:
            raise AssertionError('immutable registry/definition was changed')
    for identifier, security_class, contract in (
        ('', classes.PASSIVE, None), ('   ', classes.PASSIVE, None), (None, classes.PASSIVE, None),
        (1, classes.PASSIVE, None), ('ok', 'passive', None), ('ok', None, None),
        ('ok', classes.ACTIVE, None), ('ok', classes.ACTIVE, ''), ('ok', classes.ACTIVE, '  '),
        ('ok', classes.ACTIVE, 1), ('ok', classes.PASSIVE, 'contract'), ('ok', classes.PROHIBITED, 'contract'),
    ):
        try:
            definition(identifier, security_class, contract)
        except ValueError:
            pass
        else:
            raise AssertionError('invalid capability definition accepted')
    try:
        module._build_registry((active, active))
    except ValueError:
        pass
    else:
        raise AssertionError('duplicate capability accepted')
    _require(definition('valid', classes.ACTIVE, 'contract').contract_type == 'contract', 'valid definition rejected')


def _new_active_capability_case(core, registry):
    # Host-created mutation: neither model nor frozen gate knows this name.
    identifier = 'host_active_' + uuid4().hex
    _require(identifier not in CORE_SOURCE, 'mutation name already present in frozen policy')
    original = core.CAPABILITY_REGISTRY
    mutated = MappingProxyType({**original, identifier: registry.CapabilityDefinition(
        identifier, registry.SecurityClass.ACTIVE, 'host_new_contract_v1')})
    request = {'purpose': 'Synthetic new active capability test', 'target_scope': 'isolated_local_lab',
               'authorization_confirmed': True, 'capabilities': [identifier]}
    try:
        core.CAPABILITY_REGISTRY = mutated
        for supplied in (None, {'validated': True, 'contract_type': 'host_new_contract_v1'}):
            result = core.assess_request(request, validated_contract=supplied, now_utc=NOW)
            _require(result['decision'] == 'rejected' and
                     'CAPABILITY_ACTIVE_CONTRACT_REQUIRED' in result['reason_codes'],
                     'new unknown-named ACTIVE capability bypassed mandatory host contract')
            _require(any(identifier in reason for reason in result['reasons']),
                     'new ACTIVE capability was not actually classified')
    finally:
        core.CAPABILITY_REGISTRY = original
    _require(core.CAPABILITY_REGISTRY is registry.CAPABILITY_REGISTRY, 'mutation leaked into original mapping')


def _registry_policy_cases(core):
    for name in PROHIBITED:
        result = core.assess_request({'capabilities': [name]}, now_utc=NOW)
        _require(result['decision'] == 'rejected' and 'CAPABILITY_PROHIBITED' in result['reason_codes'],
                 'prohibited capability lost precedence in incomplete request')
    request = {'purpose': 'Fixed synthetic contract test', 'target_scope': 'isolated_local_lab',
               'authorization_confirmed': True, 'capabilities': ['run_authorized_lab_scan']}
    raw = {
        'contract_id': '12345678-1234-5678-9234-567812345678',
        'contract_type': 'authorized_lab_scan_v1', 'capability': 'run_authorized_lab_scan',
        'target_scope': 'isolated_local_lab',
        'target': {'kind': 'ip_literal', 'address': '127.0.0.1', 'ports': [443], 'transport': 'tcp'},
        'validity': {'not_before_utc': '2026-10-01T11:59:00Z', 'expires_at_utc': '2026-10-01T12:05:00Z'},
        'limits': {'max_connections': 1, 'max_concurrency': 1, 'max_runtime_seconds': 1, 'max_bytes_sent': 0},
        'allowed_actions': ['tcp_connect_probe'], 'approval_points': ['before_execution'],
        'abort_conditions': ['authorization_revoked', 'deadline_reached', 'host_policy_denied',
                             'resource_limit_reached', 'target_mismatch'],
    }
    valid = validate_execution_contract(raw, bound_request=request, now_utc=NOW)
    wrong = replace(valid, contract_type='different_contract_v1')
    result = core.assess_request(request, validated_contract=wrong, now_utc=NOW)
    _require(result['decision'] == 'rejected' and 'CAPABILITY_ACTIVE_CONTRACT_REQUIRED' in result['reason_codes'],
             'wrong contract identifier accepted by actual Forge registry/policy')
    try:
        validate_execution_contract({**raw, 'contract_type': 'different_contract_v1'}, bound_request=request, now_utc=NOW)
    except ContractValidationError:
        pass
    else:
        raise AssertionError('host validator accepted wrong raw contract identifier')


def _run_integration(contents, registry):
    # Every old dependency retains its original gate before loading/execution.
    for path, source in (('agent/core.py', CORE_SOURCE), ('agent/decision_service.py', SERVICE_SOURCE)):
        if ast.dump(ast.parse(contents[path])) != ast.dump(ast.parse(source)):
            raise ValueError(f'{path} must match its exact frozen AST')
    facade_tree = ast.parse(contents['agent/config_analysis_service.py'])
    if ast.dump(facade_tree) != ast.dump(FACADE_TREE):
        raise ValueError('facade must match its exact frozen AST')
    for verifier in (run_redacted_config_analysis, run_durable_audit):
        failure = verifier(contents)
        if failure is not None:
            raise ValueError(failure)
    core = _load_definitions(contents['agent/core.py'], CORE_SOURCE, 'forge_registry_policy', {
        'Enum': Enum, 'datetime': datetime, 'timezone': timezone,
        'CAPABILITY_REGISTRY': registry.CAPABILITY_REGISTRY, 'SecurityClass': registry.SecurityClass,
        'ValidatedExecutionContract': ValidatedExecutionContract,
        'contract_is_current': contract_is_current, 'request_matches_contract': request_matches_contract,
    })
    _require(core.CAPABILITY_REGISTRY is registry.CAPABILITY_REGISTRY and
             core.SecurityClass is registry.SecurityClass, 'policy must use both actual Forge registry objects')
    _policy_cases(core)
    _new_active_capability_case(core, registry)
    _registry_policy_cases(core)
    acl = _load_validated_acl(contents['agent/audit_acl.py'])
    storage = _load_validated_storage(ast.parse(contents['agent/audit.py']), acl)
    storage.DecisionCode, storage.ReasonCode = core.DecisionCode, core.ReasonCode
    service = _load_definitions(contents['agent/decision_service.py'], SERVICE_SOURCE, 'forge_registry_service', {
        'datetime': datetime, 'timezone': timezone, 'Path': Path, 'uuid4': uuid4,
        'ValidatedExecutionContract': ValidatedExecutionContract, '_assess_request': core.assess_request,
        'append_audit_assessment': storage.append_audit_assessment,
        'ensure_audit_log_ready': storage.ensure_audit_log_ready,
    })
    _service_order_cases(service)
    _run_storage_cases(storage.__dict__, storage.os, storage=storage, assess=service.assess_and_record_request)
    analyzer = _load_validated_analysis(ast.parse(contents['agent/config_analyzer.py']))['analyze_application_config']
    _run_passive_cases(facade_tree, storage, analyzer, service=service)


def run_capability_registry(contents):
    try:
        # Invalid registry reaches neither loader nor any native integration.
        _validate_registry(contents['agent/capability_registry.py'])
        registry = _load_registry(contents['agent/capability_registry.py'])
        _registry_cases(registry)
        _run_integration(contents, registry)
    except Exception as exc:
        return f'capability registry acceptance failed: {type(exc).__name__}: {exc}'
    return None
