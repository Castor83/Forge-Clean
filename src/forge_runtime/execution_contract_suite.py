"""Frozen validator reproduction with eight genuine Forge modules, no sockets."""

import __future__
import _strptime
import ast
from copy import deepcopy
from dataclasses import dataclass, field, fields, FrozenInstanceError
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
from ipaddress import ip_address
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from types import MappingProxyType, ModuleType
from typing import Mapping
from uuid import UUID, uuid4
from weakref import ReferenceType, ref

from .execution_contract_contract import EXPECTED_SOURCE
from . import capability_registry_suite as registry_suite
from .policy_service_suite import CORE_SOURCE, SERVICE_SOURCE, NOW, _load_definitions, _require, _service_order_cases
from .audit_acl_suite import _load_validated_acl, _run_acl_cases
from .durable_audit_suite import _load_validated_storage, run_durable_audit
from .config_analysis_suite import _load_validated_analysis, run_redacted_config_analysis
from .passive_integration_verifier import _run_passive_cases
from .recovery_suite import _run_storage_cases

DEPENDENCIES = registry_suite.REQUIRED_FILES
REQUIRED_FILES = ('agent/execution_contract.py',) + DEPENDENCIES
# Independently frozen full ASTs of the seven accepted repeat/run-1 artifacts.
DEPENDENCY_AST_SHA256 = {
    'agent/audit.py': '4a4fd16547362678f6df2e9b0aa8ae41c1a48d2acf0053c47b2f84068952b200',
    'agent/audit_acl.py': 'cab013831f78f8a1f035aa5f4b357f26ed92c0903f3f4d6e68b08a220cd4f81c',
    'agent/capability_registry.py': '721ebc314efb5a1ba11773a75f24a7cffdbccdbc1fb1c1643b59687c42e45f30',
    'agent/config_analysis_service.py': 'bc962fe3ce94dac2f8305a156348134b9ffc9345a4716acf72a225d2179a6d53',
    'agent/config_analyzer.py': 'b0f6389cd413a3963ec6409685a9626b16930448bf8540082927f40293c1eac9',
    'agent/core.py': '803a15d8796eac375f3889f9744fe0cd577a4737333a662378b37a6af22fc678',
    'agent/decision_service.py': 'f04a42295814ca2746f895ac60a408b5f76d33020208c320a2f3f0e741cca010',
}


def _validate_all_sources(contents):
    trees = {name: ast.parse(contents[name]) for name in REQUIRED_FILES}
    _require(ast.dump(trees[REQUIRED_FILES[0]]) == ast.dump(ast.parse(EXPECTED_SOURCE)),
             'validator must match its complete frozen AST')
    for name, expected in DEPENDENCY_AST_SHA256.items():
        actual = hashlib.sha256(ast.dump(trees[name]).encode('utf-8')).hexdigest()
        _require(actual == expected, f'{name} must match its complete frozen AST')
    return trees


def _load_validator(source):
    tree = ast.parse(source)
    _require(ast.dump(tree) == ast.dump(ast.parse(EXPECTED_SOURCE)), 'validator frozen AST changed')
    module = ModuleType('forge_validated_execution_contract')
    module.__dict__.update({
        'dataclass': dataclass, 'field': field, 'datetime': datetime, 'timezone': timezone, 'timedelta': timedelta,
        'hashlib': hashlib, 'ip_address': ip_address, 'json': json,
        'MappingProxyType': MappingProxyType, 'Mapping': Mapping, 'UUID': UUID,
        'ReferenceType': ReferenceType, 'ref': ref, 'RLock': RLock,
        '__builtins__': {
            '__build_class__': __build_class__, '__import__': _validator_import,
            'type': type, 'isinstance': isinstance, 'dict': dict, 'list': list,
            'str': str, 'object': object, 'len': len, 'set': set, 'frozenset': frozenset,
            'tuple': tuple, 'sorted': sorted, 'any': any, 'all': all, 'int': int, 'bool': bool,
            'id': id, 'getattr': getattr, 'hasattr': hasattr,
            'ValueError': ValueError, 'AttributeError': AttributeError, 'TypeError': TypeError,
        },
    })
    body = [n for n in tree.body if type(n) in (ast.Assign, ast.AnnAssign, ast.ClassDef, ast.FunctionDef)]
    restricted = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        exec(compile(restricted, '<forge-execution-contract>', 'exec',
                     flags=__future__.annotations.compiler_flag), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous
    return module


def _validator_import(name, globals=None, locals=None, fromlist=(), level=0):
    # CPython datetime calls these fixed host modules internally; source imports
    # are removed before execution and can never choose additional modules.
    if level == 0 and not fromlist and name in ('time', '_strptime'):
        return time if name == 'time' else _strptime
    raise ImportError('only fixed host datetime dependencies are available')


def _request():
    return {'purpose': 'Synthetic loopback lab contract', 'target_scope': 'isolated_local_lab',
            'authorization_confirmed': True, 'capabilities': ['run_authorized_lab_scan']}


def _raw_contract():
    return {
        'contract_id': '12345678-1234-5678-9234-567812345678',
        'contract_type': 'authorized_lab_scan_v1', 'capability': 'run_authorized_lab_scan',
        'target_scope': 'isolated_local_lab',
        'target': {'kind': 'ip_literal', 'address': '127.0.0.1', 'ports': [8080, 443], 'transport': 'tcp'},
        'validity': {'not_before_utc': '2026-10-01T11:59:00Z', 'expires_at_utc': '2026-10-01T12:05:00Z'},
        'limits': {'max_connections': 8, 'max_concurrency': 2, 'max_runtime_seconds': 20, 'max_bytes_sent': 0},
        'allowed_actions': ['tcp_connect_probe'], 'approval_points': ['before_execution'],
        'abort_conditions': ['authorization_revoked', 'deadline_reached', 'host_policy_denied',
                             'resource_limit_reached', 'target_mismatch'],
    }


_DEFAULT_REQUEST = object()


def _expect_invalid(module, raw, request=_DEFAULT_REQUEST, moment=NOW):
    try:
        module.validate_execution_contract(raw, bound_request=_request() if request is _DEFAULT_REQUEST else request,
                                           now_utc=moment)
    except module.ContractValidationError:
        return
    raise AssertionError('invalid raw contract accepted')


def _validator_cases(module):
    raw, request = _raw_contract(), _request()
    before = deepcopy((raw, request))
    contract = module.validate_execution_contract(raw, bound_request=request, now_utc=NOW)
    _require((raw, request) == before, 'validator changed caller data')
    _require(type(contract) is module.ValidatedExecutionContract and
             type(contract.target) is module.TargetBinding and type(contract.limits) is module.ResourceLimits,
             'validator did not produce actual Forge types')
    _require(contract.target.ports == (443, 8080) and contract.target.address == '127.0.0.1', 'target not canonical')
    _require(contract.contract_id == raw['contract_id'] and contract.request_hash ==
             hashlib.sha256(json.dumps(request, ensure_ascii=True, allow_nan=False, sort_keys=True,
                                       separators=(',', ':')).encode()).hexdigest(), 'request/UUID binding changed')
    for obj, attr, value in ((contract, 'capability', 'changed'), (contract.target, 'address', '8.8.8.8'),
                              (contract.limits, 'max_connections', 100)):
        _require(not hasattr(obj, '__dict__'), 'contract object lost slots')
        try:
            setattr(obj, attr, value)
        except (FrozenInstanceError, AttributeError, TypeError):
            pass
        else:
            raise AssertionError('contract binding is mutable')

    # Exact schemas at every nesting depth; non-string names and dict subclasses.
    for path in ((), ('target',), ('validity',), ('limits',)):
        for mode in ('missing', 'extra', 'numeric_key', 'wrong_type'):
            candidate = deepcopy(raw)
            part = candidate if not path else candidate[path[0]]
            if mode == 'missing': part.pop(next(iter(part)))
            elif mode == 'extra': part['extra'] = True
            elif mode == 'numeric_key': part[1] = True
            elif not path: candidate = []
            else: candidate[path[0]] = []
            _expect_invalid(module, candidate)
    class DictSubclass(dict): pass
    _expect_invalid(module, DictSubclass(raw))
    for key in raw:
        candidate = deepcopy(raw); candidate[key] = None; _expect_invalid(module, candidate)
    for key, values in {
        'contract_id': [1, 'invalid', '12345678-1234-5678-9234-56781234567A', '{12345678-1234-5678-9234-567812345678}'],
        'contract_type': ['other', True], 'capability': ['explain_security_concepts', True],
        'target_scope': ['owned_system', True],
    }.items():
        for value in values:
            candidate = deepcopy(raw); candidate[key] = value; _expect_invalid(module, candidate)
    for key, values in {
        'kind': ['hostname', True], 'address': ['localhost', '192.0.2.1', '::2', '', True],
        'ports': [[], (), [0], [65536], [True], [1.0], [443, 443], list(range(1, 18))],
        'transport': ['udp', True],
    }.items():
        for value in values:
            candidate = deepcopy(raw); candidate['target'][key] = value; _expect_invalid(module, candidate)
    for address in ('127.0.0.2', '::1', '0:0:0:0:0:0:0:1'):
        candidate = deepcopy(raw); candidate['target']['address'] = address
        valid = module.validate_execution_contract(candidate, bound_request=request, now_utc=NOW)
        _require(valid.target.address == str(ip_address(address)), 'loopback canonicalization failed')
    for key, maximum in (('max_connections', 64), ('max_concurrency', 4), ('max_runtime_seconds', 60)):
        for value in (0, maximum + 1, True, 1.0, '1'):
            candidate = deepcopy(raw); candidate['limits'][key] = value; _expect_invalid(module, candidate)
    for value in (1, -1, False, True, 0.0, '0'):
        candidate = deepcopy(raw); candidate['limits']['max_bytes_sent'] = value; _expect_invalid(module, candidate)
    candidate = deepcopy(raw)
    candidate['target']['ports'] = list(range(1, 17))
    candidate['limits'] = {'max_connections': 64, 'max_concurrency': 4, 'max_runtime_seconds': 60, 'max_bytes_sent': 0}
    candidate['validity'] = {'not_before_utc': '2026-10-01T12:00:00Z', 'expires_at_utc': '2026-10-01T12:15:00Z'}
    module.validate_execution_contract(candidate, bound_request=request, now_utc=NOW)
    for key in ('allowed_actions', 'approval_points', 'abort_conditions'):
        for values in ([], ['unknown'], raw[key] + [raw[key][0]], raw[key] + ['extra'], [True], tuple(raw[key])):
            candidate = deepcopy(raw); candidate[key] = values; _expect_invalid(module, candidate)
    for nb, expiry in (
        ('2026-10-01T12:00:01Z', '2026-10-01T12:05:00Z'),
        ('2026-10-01T11:59:00Z', '2026-10-01T12:00:00Z'),
        ('2026-10-01T12:00:00Z', '2026-10-01T12:15:01Z'),
        ('2026-10-01T12:00:00Z', '2026-10-01T12:00:00Z'),
        ('2026-10-01T12:00:00+00:00', '2026-10-01T12:05:00Z'),
        ('not-a-date', '2026-10-01T12:05:00Z'),
    ):
        candidate = deepcopy(raw); candidate['validity'] = {'not_before_utc': nb, 'expires_at_utc': expiry}
        _expect_invalid(module, candidate)
    for moment in (None, '2026-10-01T12:00:00Z', NOW.replace(tzinfo=None), NOW.replace(microsecond=1),
                   NOW.astimezone(timezone(timedelta(hours=2)))):
        _expect_invalid(module, raw, moment=moment)
    _require(module.contract_is_current(contract, now_utc=NOW) and
             not module.contract_is_current(contract, now_utc=NOW+timedelta(minutes=5)) and
             not module.contract_is_current(contract, now_utc=NOW-timedelta(minutes=2)), 'validity recheck failed')
    for change in ({'purpose': 'changed'}, {'authorization_confirmed': False},
                   {'capabilities': ['explain_security_concepts']}, {'target_scope': 'owned_system'}, {'extra': True}):
        changed = {**request, **change}
        if 'purpose' not in change:
            _expect_invalid(module, raw, changed)
        _require(not module.request_matches_contract(changed, contract), 'request mutation retained binding')
    for invalid_request in (None, [], {}, {**request, 'purpose': True},
                            {**request, 'authorization_confirmed': 1},
                            {**request, 'capabilities': tuple(request['capabilities'])}):
        _expect_invalid(module, raw, invalid_request)
    for supplied in (raw, {'validated': True}, object()):
        _require(not module.request_matches_contract(request, supplied) and
                 not module.contract_is_current(supplied, now_utc=NOW), 'raw claim accepted by validator helpers')
    values = {f.name: getattr(contract, f.name) for f in fields(contract)}
    try:
        module.ValidatedExecutionContract(**{**values, '_seal': object()})
    except module.ContractValidationError:
        pass
    else:
        raise AssertionError('foreign seal accepted')
    # Even possession of the actual seal must not mint another issued object.
    try:
        clone = module.ValidatedExecutionContract(**values)
    except module.ContractValidationError:
        clone = None
    else:
        _require(not module.request_matches_contract(request, clone) and
                 not module.contract_is_current(clone, now_utc=NOW), 'same-type seal copy accepted as issuer artifact')
    forged = object.__new__(module.ValidatedExecutionContract)
    for name, value in values.items(): object.__setattr__(forged, name, value)
    _require(not module.request_matches_contract(request, forged) and
             not module.contract_is_current(forged, now_utc=NOW), 'same-type forged object accepted')
    class Subclass(module.ValidatedExecutionContract): pass
    sub = object.__new__(Subclass)
    for name, value in values.items(): object.__setattr__(sub, name, value)
    _require(not module.request_matches_contract(request, sub) and
             not module.contract_is_current(sub, now_utc=NOW), 'subclass forged object accepted')
    for name, replacement in (
        ('contract_type', 'different_contract_v1'), ('expires_at_utc', '2026-10-01T12:15:00Z'),
        ('allowed_actions', frozenset({'unrestricted_action'})),
    ):
        issued = module.validate_execution_contract(raw, bound_request=request, now_utc=NOW)
        object.__setattr__(issued, name, replacement)
        _require(not module.request_matches_contract(request, issued) and
                 not module.contract_is_current(issued, now_utc=NOW), 'changed issuer artifact retained validation')
    issued = module.validate_execution_contract(raw, bound_request=request, now_utc=NOW)
    object.__setattr__(issued.limits, 'max_connections', 65)
    _require(not module.request_matches_contract(request, issued) and
             not module.contract_is_current(issued, now_utc=NOW), 'changed issued nested limits retained validation')
    return contract, clone, forged


def _mutation_missing_contract(core):
    """Separate direct regression for no contract, including the known ACTIVE."""
    result = core.assess_request(_request(), now_utc=NOW)
    _require(result['decision'] == 'rejected' and 'CAPABILITY_ACTIVE_CONTRACT_REQUIRED' in result['reason_codes'],
             'missing_contract mutation bypassed actual Forge policy')


def _mutation_raw_forged_claim(core, validator):
    """Separate direct regression for positive raw claims and same-type forgeries."""
    request = _request()
    legitimate = validator.validate_execution_contract(_raw_contract(), bound_request=request, now_utc=NOW)
    values = {f.name: getattr(legitimate, f.name) for f in fields(legitimate)}
    forged = object.__new__(validator.ValidatedExecutionContract)
    for name, value in values.items(): object.__setattr__(forged, name, value)
    for supplied in ({**_raw_contract(), 'validated': True}, {'validated': True}, forged):
        result = core.assess_request(request, validated_contract=supplied, now_utc=NOW)
        _require(result['decision'] == 'rejected' and 'CAPABILITY_ACTIVE_CONTRACT_REQUIRED' in result['reason_codes'],
                 'raw_forged_claim mutation bypassed actual Forge policy')


def _policy_validator_cases(core, registry, validator):
    passive = {'purpose': 'Review synthetic config', 'target_scope': 'owned_system',
               'authorization_confirmed': True, 'capabilities': ['review_user_provided_configuration']}
    for name in registry_suite.PASSIVE:
        _require(core.assess_request({**passive, 'capabilities': [name]}, now_utc=NOW)['decision'] == 'approved',
                 'passive Forge policy case failed')
    for value, code in (
        (None, 'REQUEST_TYPE_INVALID'), ({}, 'FIELD_MISSING'),
        ({**passive, 'authorization_confirmed': False}, 'AUTHORIZATION_REQUIRED'),
        ({**passive, 'authorization_confirmed': 1}, 'AUTHORIZATION_REQUIRED'),
        ({**passive, 'extra': True}, 'FIELD_UNKNOWN'), ({**passive, 1: True}, 'FIELD_NAME_INVALID'),
        ({**passive, 'capabilities': []}, 'CAPABILITIES_INVALID'),
        ({**passive, 'capabilities': [None]}, 'CAPABILITY_ITEM_INVALID'),
        ({**passive, 'capabilities': passive['capabilities'] * 2}, 'CAPABILITY_DUPLICATE'),
        ({**passive, 'purpose': ''}, 'PURPOSE_INVALID'),
        ({**passive, 'target_scope': 'foreign'}, 'TARGET_SCOPE_INVALID'),
    ):
        before = deepcopy(value)
        result = core.assess_request(value, now_utc=NOW)
        _require(result['decision'] == 'needs_clarification' and code in result['reason_codes'], 'passive policy validation changed')
        _require(before == value and result == core.assess_request(value, now_utc=NOW), 'policy mutation or nondeterminism')
        _require(len(result['reasons']) == len(result['reason_codes']), 'reason/code pairing changed')
    for name in registry_suite.PROHIBITED:
        result = core.assess_request({'capabilities': [name]}, now_utc=NOW)
        _require(result['decision'] == 'rejected' and 'CAPABILITY_PROHIBITED' in result['reason_codes'],
                 'prohibited precedence failed')
    mixed = {**passive, 'authorization_confirmed': False,
             'capabilities': ['unknown', 'obtain_credentials', 'run_authorized_lab_scan']}
    _require('CAPABILITY_PROHIBITED' in core.assess_request(mixed, now_utc=NOW)['reason_codes'], 'mixed prohibited precedence failed')
    _require(core.assess_request({**passive, 'capabilities': ['unknown']}, now_utc=NOW)['decision'] == 'needs_clarification',
             'unknown capability accepted')
    _mutation_missing_contract(core)
    _mutation_raw_forged_claim(core, validator)
    registry_suite._new_active_capability_case(core, registry)
    contract = validator.validate_execution_contract(_raw_contract(), bound_request=_request(), now_utc=NOW)
    _require(core.assess_request(_request(), validated_contract=contract, now_utc=NOW)['decision'] == 'approved',
             'actual Forge validator contract not integrated with actual Forge policy')
    for req, moment in (({**_request(), 'purpose': 'changed'}, NOW),
                        ({**_request(), 'authorization_confirmed': False}, NOW),
                        (_request(), NOW+timedelta(minutes=6)), (_request(), NOW-timedelta(minutes=2))):
        _require(core.assess_request(req, validated_contract=contract, now_utc=moment)['decision'] == 'rejected',
                 'unbound/expired active contract approved')


def _run_integration(contents, trees, validator):
    registry = registry_suite._load_registry(contents['agent/capability_registry.py'])
    registry_suite._registry_cases(registry)
    core = _load_definitions(contents['agent/core.py'], CORE_SOURCE, 'forge_validator_policy', {
        'Enum': Enum, 'datetime': datetime, 'timezone': timezone,
        'CAPABILITY_REGISTRY': registry.CAPABILITY_REGISTRY, 'SecurityClass': registry.SecurityClass,
        'ValidatedExecutionContract': validator.ValidatedExecutionContract,
        'contract_is_current': validator.contract_is_current,
        'request_matches_contract': validator.request_matches_contract,
    })
    _require(core.CAPABILITY_REGISTRY is registry.CAPABILITY_REGISTRY and core.SecurityClass is registry.SecurityClass and
             core.ValidatedExecutionContract is validator.ValidatedExecutionContract and
             core.contract_is_current is validator.contract_is_current and
             core.request_matches_contract is validator.request_matches_contract, 'actual Forge identity binding failed')
    _policy_validator_cases(core, registry, validator)
    acl = _load_validated_acl(contents['agent/audit_acl.py'])
    _run_acl_cases(acl)
    storage = _load_validated_storage(trees['agent/audit.py'], acl)
    storage.DecisionCode, storage.ReasonCode = core.DecisionCode, core.ReasonCode
    service = _load_definitions(contents['agent/decision_service.py'], SERVICE_SOURCE, 'forge_validator_service', {
        'datetime': datetime, 'timezone': timezone, 'Path': Path, 'uuid4': uuid4,
        'ValidatedExecutionContract': validator.ValidatedExecutionContract, '_assess_request': core.assess_request,
        'append_audit_assessment': storage.append_audit_assessment,
        'ensure_audit_log_ready': storage.ensure_audit_log_ready,
    })
    _require(service.ValidatedExecutionContract is validator.ValidatedExecutionContract and
             service._assess_request is core.assess_request, 'actual Forge service/validator identity failed')
    _service_order_cases(service)
    _run_storage_cases(storage.__dict__, storage.os, storage=storage, assess=service.assess_and_record_request)
    # Observe the genuine active classification through the real audited service.
    with TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
        contract = validator.validate_execution_contract(_raw_contract(), bound_request=_request(), now_utc=NOW)
        result = service.assess_and_record_request(_request(), audit_path=Path(temp)/'active.jsonl',
                                                  validated_contract=contract, now_utc=NOW)
        _require(result['decision'] == 'approved', 'Forge service lost issued validator binding')
    analyzer = _load_validated_analysis(trees['agent/config_analyzer.py'])['analyze_application_config']
    _run_passive_cases(trees['agent/config_analysis_service.py'], storage, analyzer, service=service)
    return {'registry': registry, 'core': core, 'validator': validator,
            'acl': acl, 'storage': storage, 'service': service, 'analyzer': analyzer}


def run_execution_contract(contents):
    try:
        # No generated loader or native integration is reached until all eight gates pass.
        trees = _validate_all_sources(contents)
        validator = _load_validator(contents['agent/execution_contract.py'])
        _validator_cases(validator)
        for verifier in (run_redacted_config_analysis, run_durable_audit):
            failure = verifier(contents)
            _require(failure is None, failure)
        _run_integration(contents, trees, validator)
    except Exception as exc:
        return f'execution contract acceptance failed: {type(exc).__name__}: {exc}'
    return None
