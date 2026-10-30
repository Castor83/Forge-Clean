"""Nine frozen Forge artifacts; actual active path with only fake sockets."""

import __future__
import ast
from copy import copy, deepcopy
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from ipaddress import ip_address
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, RLock
from time import monotonic
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from weakref import ReferenceType, ref

from .active_execution_contract import EXPECTED_SOURCE
from . import execution_contract_suite as validator_suite
from .policy_service_suite import _require

DEPENDENCIES = validator_suite.REQUIRED_FILES
REQUIRED_FILES = ('agent/active_scan.py',) + DEPENDENCIES
NOW = validator_suite.NOW
_EXPECTED_AST = ast.dump(ast.parse(EXPECTED_SOURCE))


def _validate_all_sources(contents):
    active_tree = ast.parse(contents['agent/active_scan.py'])
    _require(ast.dump(active_tree) == _EXPECTED_AST, 'active execution must match its complete frozen AST')
    return {'agent/active_scan.py': active_tree, **validator_suite._validate_all_sources(contents)}


class _FakeSocket:
    """Host test double: no real socket module, DNS API, or payload API exists."""
    AF_INET, AF_INET6, SOCK_STREAM = 2, 23, 1

    def __init__(self):
        self.events = []
        self.results = []
        self.before_socket = None
        self.socket_error = None

    def socket(self, family, kind):
        if self.before_socket is not None:
            self.before_socket()
        self.events.append(('socket', family, kind))
        if self.socket_error is not None:
            raise self.socket_error
        owner = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                owner.events.append(('close',))

            def settimeout(self, timeout):
                owner.events.append(('timeout', timeout))

            def connect_ex(self, endpoint):
                owner.events.append(('connect', endpoint))
                result = owner.results.pop(0) if owner.results else 0
                if isinstance(result, Exception):
                    raise result
                return result

        return Connection()


def _load_active(source, validator, service):
    tree = ast.parse(source)
    _require(ast.dump(tree) == _EXPECTED_AST, 'active loader requires complete frozen AST')
    module = ModuleType('forge_validated_active_execution')
    module.__dict__.update({
        'dataclass': dataclass, 'field': field, 'datetime': datetime, 'timezone': timezone,
        'Enum': Enum, 'ip_address': ip_address, 'Path': Path, 'socket': _FakeSocket(),
        'Event': Event, 'RLock': RLock, 'monotonic': monotonic, 'ReferenceType': ReferenceType, 'ref': ref,
        'ValidatedExecutionContract': validator.ValidatedExecutionContract,
        'contract_is_current': validator.contract_is_current,
        'assess_and_record_request': service.assess_and_record_request,
        '__builtins__': {
            '__build_class__': __build_class__, '__import__': validator_suite._validator_import,
            'type': type, 'str': str, 'int': int, 'bool': bool, 'float': float, 'object': object,
            'tuple': tuple, 'list': list, 'dict': dict, 'id': id, 'getattr': getattr,
            'property': property,
            'min': min, 'max': max, 'ValueError': ValueError, 'TypeError': TypeError,
            'OSError': OSError, 'AttributeError': AttributeError,
        },
    })
    body = [node for node in tree.body if type(node) in (ast.Assign, ast.AnnAssign, ast.ClassDef, ast.FunctionDef)]
    definitions = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        exec(compile(definitions, '<forge-active-execution>', 'exec',
                     flags=__future__.annotations.compiler_flag), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous
    _require(module.ValidatedExecutionContract is validator.ValidatedExecutionContract and
             module.contract_is_current is validator.contract_is_current and
             module.assess_and_record_request is service.assess_and_record_request,
             'active path lost actual Forge validator/service identities')
    return module


def _contract(validator, *, raw=None):
    return validator.validate_execution_contract(
        validator_suite._raw_contract() if raw is None else raw,
        bound_request=validator_suite._request(), now_utc=NOW)


def _expect_error(exceptions, operation, message):
    try:
        operation()
    except exceptions:
        return
    raise AssertionError(message)


def _probe(active, contract, *, address='127.0.0.1', port=443):
    guard = active.ExecutionGuard(contract)
    status, authorization = guard.reserve_probe(address=address, port=port, transport='tcp')
    _require(status is None and type(authorization) is active._AuthorizedProbe, 'valid guard failed to issue probe')
    return authorization


def _clone(template, *, seal=None, cls=None, omit_seal=False):
    forged = object.__new__(cls or type(template))
    for item in fields(template):
        value = getattr(template, item.name)
        if item.name == '_seal':
            if omit_seal:
                continue
            value = seal
        object.__setattr__(forged, item.name, value)
    return forged


def _deny_probe(active, candidate):
    before = len(active.socket.events)
    _expect_error((TypeError, ValueError), lambda: active._tcp_connect_probe(candidate, timeout_seconds=1),
                  'forged/copied/replayed probe authorization reached socket')
    _require(len(active.socket.events) == before, 'probe denial occurred after socket contact')


def _audit_path(directory, name):
    parent = directory/name
    parent.mkdir()
    return parent/'audit.jsonl'


def _mutation_missing_contract(active, directory):
    before = len(active.socket.events)
    result = active.run_authorized_lab_scan(validator_suite._request(), None,
                                           audit_path=_audit_path(directory, 'missing-contract'))
    _require(not result.completed and result.attempts == 0 and
             result.status_code == 'EXECUTION_HOST_POLICY_DENIED', 'missing_contract mutation bypassed active host')
    _require(len(active.socket.events) == before, 'missing_contract mutation contacted socket')


def _mutation_raw_forged_claim(active, validator, directory):
    genuine = _contract(validator)
    for index, candidate in enumerate((
        {**validator_suite._raw_contract(), 'validated': True}, {'validated': True},
        _clone(genuine, seal=object()), _clone(genuine, seal=genuine._seal),
        copy(genuine), replace(genuine),
    )):
        before = len(active.socket.events)
        result = active.run_authorized_lab_scan(validator_suite._request(), candidate,
                                               audit_path=_audit_path(directory, f'raw-forged-{index}'))
        _require(not result.completed and result.attempts == 0 and
                 result.status_code == 'EXECUTION_HOST_POLICY_DENIED', 'raw_forged_claim mutation bypassed active host')
        _require(len(active.socket.events) == before, 'raw_forged_claim mutation contacted socket')


def _guard_cases(active, validator):
    contract = _contract(validator)
    codes = active.ExecutionStatusCode
    before = len(active.socket.events)
    guard = active.ExecutionGuard(contract)
    for address, port, transport in (('127.0.0.2', 443, 'tcp'), ('localhost', 443, 'tcp'),
                                     ('127.0.0.1', 22, 'tcp'), ('127.0.0.1', 443, 'udp'),
                                     ('127.0.0.1', True, 'tcp')):
        status, grant = guard.reserve_probe(address=address, port=port, transport=transport)
        _require(status is codes.TARGET_MISMATCH and grant is None and guard.attempts == 0,
                 'target mismatch reserved an attempt')
    revoked = Event(); revoked.set()
    guard = active.ExecutionGuard(contract, abort_event=revoked)
    status, grant = guard.reserve_probe(address='127.0.0.1', port=443, transport='tcp')
    _require(status is codes.AUTHORIZATION_REVOKED and grant is None and guard.attempts == 0,
             'revocation did not abort before reservation')
    for clock, value, expected in (
        ('_utc_now', NOW+timedelta(minutes=5), codes.DEADLINE_REACHED),
        ('_monotonic', 400.0, codes.DEADLINE_REACHED),
        ('_monotonic', 120.0, codes.RESOURCE_LIMIT_REACHED),
    ):
        guard = active.ExecutionGuard(contract)
        with patch.object(active, clock, return_value=value):
            status, grant = guard.reserve_probe(address='127.0.0.1', port=443, transport='tcp')
            _require(guard.remaining_seconds() >= 0, 'remaining runtime went negative')
        _require(status is expected and grant is None and guard.attempts == 0, 'deadline/resource gate did not abort')
    _require(len(active.socket.events) == before, 'guard denial contacted socket')
    raw = validator_suite._raw_contract(); raw['limits']['max_connections'] = 1
    guard = active.ExecutionGuard(_contract(validator, raw=raw))
    status, authorization = guard.reserve_probe(address='127.0.0.1', port=443, transport='tcp')
    _require(status is None and authorization is not None and guard.attempts == 1, 'budget reservation failed')
    status, authorization = guard.reserve_probe(address='127.0.0.1', port=8080, transport='tcp')
    _require(status is codes.RESOURCE_LIMIT_REACHED and authorization is None and guard.attempts == 1,
             'connection budget allowed an excess reservation')


def _sealed_cases(active, validator):
    contract = _contract(validator)
    original = _probe(active, contract)
    class ProbeSubclass(active._AuthorizedProbe): pass
    candidates = [object(), ('127.0.0.1', 443), SimpleNamespace(**{f.name: getattr(original, f.name) for f in fields(original)}),
                  _clone(original, seal=object()), _clone(original, omit_seal=True),
                  _clone(original, seal=original._seal), _clone(original, seal=original._seal, cls=ProbeSubclass),
                  copy(original), replace(original)]
    candidates.append(active._AuthorizedProbe(address=original.address, port=original.port,
                                              transport=original.transport, _seal=original._seal))
    for candidate in candidates:
        _deny_probe(active, candidate)
    before = len(active.socket.events)
    _require(active._tcp_connect_probe(original, timeout_seconds=1), 'genuine sealed probe failed')
    _require(len(active.socket.events) == before + 4, 'genuine probe did not use exact fake socket path')
    _deny_probe(active, copy(original)); _deny_probe(active, replace(original))
    object.__setattr__(original, '_used', False); _deny_probe(active, original)
    for attribute, replacement in (('port', 22), ('address', '127.0.0.2'), ('transport', 'udp'), ('_seal', object())):
        original = _probe(active, contract)
        saved = getattr(original, attribute)
        object.__setattr__(original, attribute, replacement)
        _deny_probe(active, original)
        _require(original._used and id(original) not in active._ISSUED_PROBES, 'altered grant did not burn issuance')
        object.__setattr__(original, attribute, saved); object.__setattr__(original, '_used', False)
        _deny_probe(active, original)
    # Isolated host fault injection bypasses only the preceding content gate.
    # Thus the helper's own loopback/TCP checks are tested independently.
    for attribute, replacement in (('address', '192.0.2.1'), ('transport', 'udp')):
        original = _probe(active, contract)
        object.__setattr__(original, attribute, replacement)
        with active._PROBE_ISSUANCE_LOCK:
            entry = active._ISSUED_PROBES[id(original)]
            active._ISSUED_PROBES[id(original)] = (entry[0], (original.address, original.port, original.transport))
        before = len(active.socket.events)
        try:
            active._tcp_connect_probe(original, timeout_seconds=1)
        except ValueError as exc:
            _require('fixed host boundary' in str(exc), 'helper own boundary was not reached')
        else:
            raise AssertionError('helper allowed nonloopback/nonTCP host fixture')
        _require(len(active.socket.events) == before and original._used, 'helper denial contacted socket or preserved grant')
    original = _probe(active, contract)
    active.socket.socket_error = OSError('synthetic socket failure')
    try:
        _expect_error(OSError, lambda: active._tcp_connect_probe(original, timeout_seconds=1), 'socket failure was swallowed')
    finally:
        active.socket.socket_error = None
    _deny_probe(active, original); _deny_probe(active, replace(original))


def _imitated_guard_cases(active, validator, directory):
    genuine = _contract(validator)
    guard = active.ExecutionGuard(genuine)
    attributes = {name: getattr(guard, name) for name in guard.__slots__ if name != '__weakref__'}
    forged = object.__new__(active.ExecutionGuard)
    for name, value in attributes.items(): object.__setattr__(forged, name, value)
    for candidate in (SimpleNamespace(**attributes, current_abort_status=lambda: None), forged, copy(guard)):
        _expect_error(TypeError, lambda: active.ExecutionGuard.reserve_probe(candidate, address='127.0.0.1',
                       port=443, transport='tcp'), 'imitated guard minted genuine authorization')
        _expect_error(TypeError, lambda: active.ExecutionGuard.current_abort_status(candidate),
                      'imitated guard bypassed current status check')
        _require(candidate._attempts == 0, 'imitated guard reserved budget')
    object.__setattr__(guard, '_contract', _contract(validator))
    _expect_error(TypeError, lambda: guard.reserve_probe(address='127.0.0.1', port=443, transport='tcp'),
                  'genuine guard accepted reflected contract replacement')
    template = _probe(active, genuine)
    for index, candidate in enumerate((_clone(template, seal=object()), _clone(template, seal=template._seal))):
        imitated = SimpleNamespace(attempts=1, reserve_probe=lambda **_kwargs: (None, candidate),
                                  remaining_seconds=lambda: 10.0)
        before = len(active.socket.events)
        with patch.object(active, 'ExecutionGuard', return_value=imitated):
            _expect_error(TypeError, lambda: active.run_authorized_lab_scan(validator_suite._request(), genuine,
                          audit_path=_audit_path(directory, f'imitated-guard-{index}')),
                          'imitated guard grant passed actual scan helper')
        _require(len(active.socket.events) == before, 'imitated guard grant contacted socket')


def _scan_audit_cases(active, validator, storage, directory):
    contract = _contract(validator)
    audit_path = _audit_path(directory, 'completed')
    first = len(active.socket.events)
    observed = []
    def require_durable_audit():
        _require(storage.verify_audit_log(audit_path), 'audit was not verified before socket contact')
        entries = storage._read_and_verify(audit_path)
        _require(len(entries) == 1 and entries[0].decision_code == 'REQUEST_APPROVED', 'active decision not audited before socket')
        _require(not storage._pending_path(audit_path).exists(), 'pending audit released network action')
        observed.append('durable-audit')
    active.socket.before_socket = require_durable_audit
    active.socket.results = [0, 111]
    try:
        result = active.run_authorized_lab_scan(validator_suite._request(), contract, audit_path=audit_path)
    finally:
        active.socket.before_socket = None
    _require(result.completed and result.attempts == 2 and
             [(p.port, p.status) for p in result.probes] == [(443, 'open'), (8080, 'closed')], 'fixed active scan result changed')
    events = active.socket.events[first:]
    _require([event[1] for event in events if event[0] == 'connect'] == [('127.0.0.1', 443), ('127.0.0.1', 8080)] and
             observed == ['durable-audit', 'durable-audit'], 'actual bound port/audit path changed')
    raw = validator_suite._raw_contract(); raw['target']['address'] = '::1'; raw['target']['ports'] = [443]
    first = len(active.socket.events)
    ipv6 = active.run_authorized_lab_scan(validator_suite._request(), _contract(validator, raw=raw),
                                          audit_path=_audit_path(directory, 'ipv6'))
    _require(ipv6.completed and ('socket', active.socket.AF_INET6, active.socket.SOCK_STREAM) in active.socket.events[first:] and
             ('connect', ('::1', 443, 0, 0)) in active.socket.events[first:], 'IPv6 family/endpoint changed')
    raw = validator_suite._raw_contract(); raw['limits']['max_connections'] = 1
    first = len(active.socket.events)
    result = active.run_authorized_lab_scan(validator_suite._request(), _contract(validator, raw=raw),
                                           audit_path=_audit_path(directory, 'budget'))
    _require(not result.completed and result.attempts == 1 and result.status_code == 'EXECUTION_RESOURCE_LIMIT_REACHED' and
             len([e for e in active.socket.events[first:] if e[0] == 'socket']) == 1, 'scan exceeded connection budget')
    for kind in ('corrupt', 'pending', 'fsync'):
        path = _audit_path(directory, f'audit-{kind}')
        if kind == 'corrupt':
            active.assess_and_record_request(validator_suite._request(), audit_path=path,
                                             validated_contract=contract, now_utc=NOW)
            entry = json.loads(path.read_bytes()); entry['policy_version'] = 'tampered'
            path.write_text(json.dumps(entry)+'\n', encoding='utf-8')
        elif kind == 'pending':
            storage.ensure_audit_log_ready(path)
            storage._pending_path(path).write_bytes(b'host-synthetic-pending')
            storage.harden_audit_recovery_marker(path)
        before = len(active.socket.events)
        if kind == 'fsync':
            with patch.object(storage.os, 'fsync', side_effect=OSError('synthetic fsync failure')):
                _expect_error(OSError, lambda: active.run_authorized_lab_scan(validator_suite._request(), contract,
                              audit_path=path), 'failed durable audit released active operation')
        else:
            _expect_error((ValueError, storage.AuditRecoveryRequired),
                          lambda: active.run_authorized_lab_scan(validator_suite._request(), contract, audit_path=path),
                          'invalid/pending audit released active operation')
        _require(len(active.socket.events) == before, 'audit denial contacted socket')
    before = len(active.socket.events)
    with patch.object(active.ExecutionGuard, 'remaining_seconds', return_value=0):
        result = active.run_authorized_lab_scan(validator_suite._request(), contract, audit_path=_audit_path(directory, 'remaining-zero'))
    _require(not result.completed and result.attempts == 1 and result.status_code == 'EXECUTION_RESOURCE_LIMIT_REACHED' and
             len(active.socket.events) == before, 'zero remaining time reached socket')
    with patch.object(active, '_utc_now', side_effect=[NOW, NOW, NOW, NOW+timedelta(minutes=5)]):
        before = len(active.socket.events)
        result = active.run_authorized_lab_scan(validator_suite._request(), contract, audit_path=_audit_path(directory, 'expiry-after-probe'))
    _require(result.status_code == 'EXECUTION_DEADLINE_REACHED' and result.attempts == 1 and
             len([e for e in active.socket.events[before:] if e[0] == 'socket']) == 1, 'post-contact expiry did not abort next probe')
    active.socket.socket_error = OSError('synthetic scan socket failure')
    try:
        result = active.run_authorized_lab_scan(validator_suite._request(), contract, audit_path=_audit_path(directory, 'socket-error'))
    finally:
        active.socket.socket_error = None
    _require(result.status_code == 'EXECUTION_INTERNAL_ERROR' and result.attempts == 1 and not result.completed,
             'scan socket error did not fail closed')


def _active_recovery_cases(active, validator, storage, directory):
    """Recover a genuine interrupted active audit, then run a new active scan."""
    proofs = []
    for kind, expected_status, action in (
        ('absent', storage.AuditRecoveryStatus.ENTRY_ABSENT, storage.AuditRecoveryAction.APPEND_MISSING),
        ('present', storage.AuditRecoveryStatus.ENTRY_PRESENT, storage.AuditRecoveryAction.ACKNOWLEDGE_PRESENT),
    ):
        path = _audit_path(directory, f'active-recovery-{kind}')
        before_contact = len(active.socket.events)
        contract = _contract(validator)
        if kind == 'absent':
            with patch.object(storage, '_append_entry_line', side_effect=OSError('synthetic interrupted active audit append')):
                _expect_error(OSError, lambda: active.run_authorized_lab_scan(validator_suite._request(), contract,
                              audit_path=path), 'interrupted active audit append released execution')
        else:
            original_fsync = storage.os.fsync
            syncs = 0
            def fail_log_sync(descriptor):
                nonlocal syncs
                syncs += 1
                if syncs == 2:
                    raise OSError('synthetic interrupted active audit log sync')
                original_fsync(descriptor)
            with patch.object(storage.os, 'fsync', side_effect=fail_log_sync):
                _expect_error(OSError, lambda: active.run_authorized_lab_scan(validator_suite._request(), contract,
                              audit_path=path), 'interrupted active audit sync released execution')
            _require(syncs == 2, 'active recovery fixture did not interrupt the log durability check')
        marker = storage._pending_path(path)
        _require(marker.is_file() and len(active.socket.events) == before_contact,
                 'interrupted active audit lost its pending marker or contacted socket')
        original_bytes = (path.read_bytes(), marker.read_bytes())
        report = storage.inspect_audit_recovery(path)
        _require(report.status is expected_status and report.entry_hash is not None,
                 'genuine active pending marker was not recoverable in its expected state')
        _require(original_bytes == (path.read_bytes(), marker.read_bytes()), 'active recovery inspection changed bytes')
        # The actual active entry path remains blocked while the real marker exists.
        _expect_error(storage.AuditRecoveryRequired,
                      lambda: active.run_authorized_lab_scan(validator_suite._request(), contract, audit_path=path),
                      'unresolved valid active pending marker released execution')
        _require(len(active.socket.events) == before_contact and
                 original_bytes == (path.read_bytes(), marker.read_bytes()), 'active pending denial changed evidence or contacted socket')
        _expect_error(storage.AuditRecoveryRequired,
                      lambda: storage.resolve_audit_recovery(path, expected_entry_hash='0'*64, action=action),
                      'active recovery accepted the wrong confirmation hash')
        _require(original_bytes == (path.read_bytes(), marker.read_bytes()), 'rejected active recovery changed evidence')
        storage.resolve_audit_recovery(path, expected_entry_hash=report.entry_hash, action=action)
        recovered = storage._read_and_verify(path)
        _require(not marker.exists() and len(recovered) == 1 and recovered[0].entry_hash == report.entry_hash,
                 'active recovery did not preserve exactly its confirmed audit entry')
        observed = []
        def require_resumed_audit():
            _require(storage.verify_audit_log(path), 'resumed active audit was not durable before socket')
            entries = storage._read_and_verify(path)
            _require(len(entries) == 2 and entries[0].entry_hash == report.entry_hash and
                     entries[-1].decision_code == 'REQUEST_APPROVED' and
                     entries[-1].reason_codes == ('CAPABILITY_ACTIVE_CONTRACT_VALIDATED',) and
                     entries[-1].request_hash == contract.request_hash and not marker.exists(),
                     'resumed active scan reached socket before its new confirmed audit entry')
            observed.append('durable-resumed-active-audit')
        first = len(active.socket.events)
        active.socket.before_socket = require_resumed_audit
        try:
            resumed = active.run_authorized_lab_scan(validator_suite._request(), _contract(validator), audit_path=path)
        finally:
            active.socket.before_socket = None
        contacts = [event[1] for event in active.socket.events[first:] if event[0] == 'connect']
        _require(resumed.completed and resumed.status_code == 'EXECUTION_COMPLETED' and resumed.attempts == 2 and
                 contacts == [('127.0.0.1', 443), ('127.0.0.1', 8080)] and
                 observed == ['durable-resumed-active-audit', 'durable-resumed-active-audit'],
                 'recovered active scan did not actually complete its audited bound fake socket path')
        proofs.append({'recovery_status': expected_status.value, 'action': action.value,
                       'resumed_socket_contacts': len(contacts), 'audit_entries': len(storage._read_and_verify(path))})
    return proofs


def _active_cases(active, graph):
    validator, storage = graph['validator'], graph['storage']
    with patch.object(active, '_utc_now', return_value=NOW), patch.object(active, '_monotonic', return_value=100.0):
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[2]) as temp:
            directory = Path(temp)
            _mutation_missing_contract(active, directory)
            _mutation_raw_forged_claim(active, validator, directory)
            _guard_cases(active, validator)
            _sealed_cases(active, validator)
            _imitated_guard_cases(active, validator, directory)
            _scan_audit_cases(active, validator, storage, directory)
            _active_recovery_cases(active, validator, storage, directory)


def run_active_execution(contents):
    try:
        # Full nine-file admission completes before any generated loader runs.
        trees = _validate_all_sources(contents)
        validator = validator_suite._load_validator(contents['agent/execution_contract.py'])
        validator_suite._validator_cases(validator)
        graph = validator_suite._run_integration(contents, trees, validator)
        active = _load_active(contents['agent/active_scan.py'], validator, graph['service'])
        _active_cases(active, graph)
    except Exception as exc:
        return f'active execution acceptance failed: {type(exc).__name__}: {exc}'
    return None
