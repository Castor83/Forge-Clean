"""Frozen policy/service acceptance with native six-module integration."""

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import ModuleType
import time
from unittest.mock import patch
from uuid import uuid4

from src.obsidian_policy.capability_registry import CAPABILITY_REGISTRY, SecurityClass
from src.obsidian_policy.execution_contract import (
    ValidatedExecutionContract, contract_is_current, request_matches_contract,
    validate_execution_contract,
)
from .policy_service_contract import CORE_SOURCE, SERVICE_SOURCE
from .partitioned_audit_suite import _compose, partition_source
from .audit_acl_suite import _load_validated_acl
from .audited_config_analysis_suite import _EXPECTED_TREE as FACADE_TREE
from .config_analysis_suite import _load_validated_analysis, run_redacted_config_analysis
from .durable_audit_suite import _load_validated_storage, run_durable_audit
from .passive_integration_verifier import _run_passive_cases
from .recovery_suite import _run_storage_cases


CORE_PARTS = partition_source(CORE_SOURCE)
SERVICE_PARTS = partition_source(SERVICE_SOURCE)
CORE_FILES = tuple(f"parts/core_{i}.txt" for i in range(1, len(CORE_PARTS) + 1))
SERVICE_FILES = tuple(f"parts/decision_service_{i}.txt" for i in range(1, len(SERVICE_PARTS) + 1))
DEPENDENCIES = (
    "agent/audit.py", "agent/audit_acl.py", "agent/config_analysis_service.py",
    "agent/config_analyzer.py",
)
REQUIRED_FILES = CORE_FILES + SERVICE_FILES + DEPENDENCIES
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def run_policy_service(contents):
    try:
        core, failure = _compose(contents, CORE_FILES, CORE_PARTS)
        if failure is not None:
            return failure
        service, failure = _compose(contents, SERVICE_FILES, SERVICE_PARTS)
        if failure is not None:
            return failure
        return verify_policy_service({**contents, "agent/core.py": core, "agent/decision_service.py": service})
    except Exception as exc:
        return f"policy/service fragment acceptance failed: {type(exc).__name__}: {exc}"


def _load_definitions(source, expected, name, dependencies):
    tree = ast.parse(source)
    if ast.dump(tree) != ast.dump(ast.parse(expected)):
        raise ValueError(f"{name} must match its exact frozen AST")
    module = ModuleType(name)
    module.__dict__.update(dependencies)
    module.__dict__["__builtins__"] = {
        "type": type, "isinstance": isinstance, "dict": dict, "list": list,
        "str": str, "bool": bool, "object": object, "len": len, "set": set,
        "frozenset": frozenset, "sorted": sorted, "enumerate": enumerate,
        "ValueError": ValueError, "__build_class__": __build_class__,
        "__import__": _time_import,
    }
    body = [node for node in tree.body if type(node) in (ast.Assign, ast.ClassDef, ast.FunctionDef)]
    definitions = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(definitions, f"<{name}>", "exec"), module.__dict__)
    return module


def _time_import(name, globals=None, locals=None, fromlist=(), level=0):
    # CPython datetime.strftime requests time through the caller's builtins.
    if name != "time" or level != 0 or fromlist:
        raise ImportError("only the host time module is available to datetime")
    return time


def verify_policy_service(contents):
    """Verify assembled actual outputs; never import their source modules."""
    try:
        # Both new modules must pass before any generated definition is loaded.
        for path, expected in (("agent/core.py", CORE_SOURCE), ("agent/decision_service.py", SERVICE_SOURCE)):
            if ast.dump(ast.parse(contents[path])) != ast.dump(ast.parse(expected)):
                return f"{path} must match its exact frozen AST"
        facade_tree = ast.parse(contents["agent/config_analysis_service.py"])
        if ast.dump(facade_tree) != ast.dump(FACADE_TREE):
            return "facade must match its exact frozen AST"
        for verifier in (run_redacted_config_analysis, run_durable_audit):
            failure = verifier(contents)
            if failure is not None:
                return failure
        core = _load_definitions(contents["agent/core.py"], CORE_SOURCE, "forge_policy", {
            "Enum": Enum, "datetime": datetime, "timezone": timezone,
            "CAPABILITY_REGISTRY": CAPABILITY_REGISTRY, "SecurityClass": SecurityClass,
            "ValidatedExecutionContract": ValidatedExecutionContract,
            "contract_is_current": contract_is_current,
            "request_matches_contract": request_matches_contract,
        })
        _policy_cases(core)
        acl = _load_validated_acl(contents["agent/audit_acl.py"])
        storage = _load_validated_storage(ast.parse(contents["agent/audit.py"]), acl)
        storage.DecisionCode = core.DecisionCode
        storage.ReasonCode = core.ReasonCode
        service = _load_definitions(contents["agent/decision_service.py"], SERVICE_SOURCE, "forge_service", {
            "datetime": datetime, "timezone": timezone, "Path": Path, "uuid4": uuid4,
            "ValidatedExecutionContract": ValidatedExecutionContract,
            "_assess_request": core.assess_request,
            "append_audit_assessment": storage.append_audit_assessment,
            "ensure_audit_log_ready": storage.ensure_audit_log_ready,
        })
        _service_order_cases(service)
        _run_storage_cases(storage.__dict__, storage.os, storage=storage, assess=service.assess_and_record_request)
        analyzer = _load_validated_analysis(ast.parse(contents["agent/config_analyzer.py"]))["analyze_application_config"]
        _run_passive_cases(facade_tree, storage, analyzer, service=service)
    except Exception as exc:
        return f"policy/service acceptance failed: {type(exc).__name__}: {exc}"
    return None


def _require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def _policy_cases(core):
    request = {"purpose": "Review synthetic config", "target_scope": "owned_system",
               "authorization_confirmed": True, "capabilities": ["review_user_provided_configuration"]}
    cases = [
        (request, "approved", "REQUEST_WITHIN_DEFENSIVE_SCOPE"),
        (None, "needs_clarification", "REQUEST_TYPE_INVALID"),
        ({}, "needs_clarification", "FIELD_MISSING"),
        ({**request, "capabilities": ["unknown"]}, "needs_clarification", "CAPABILITY_UNKNOWN"),
        ({**request, "authorization_confirmed": False}, "needs_clarification", "AUTHORIZATION_REQUIRED"),
        ({**request, "authorization_confirmed": 1}, "needs_clarification", "AUTHORIZATION_REQUIRED"),
        ({**request, "extra": True}, "needs_clarification", "FIELD_UNKNOWN"),
        ({**request, 1: "unexpected"}, "needs_clarification", "FIELD_NAME_INVALID"),
        ({**request, "capabilities": []}, "needs_clarification", "CAPABILITIES_INVALID"),
        ({**request, "capabilities": [None]}, "needs_clarification", "CAPABILITY_ITEM_INVALID"),
        ({**request, "capabilities": request["capabilities"] * 2}, "needs_clarification", "CAPABILITY_DUPLICATE"),
        ({**request, "purpose": ""}, "needs_clarification", "PURPOSE_INVALID"),
        ({**request, "target_scope": "foreign"}, "needs_clarification", "TARGET_SCOPE_INVALID"),
    ]
    for name, definition in CAPABILITY_REGISTRY.items():
        state, code = {
            SecurityClass.PASSIVE: ("approved", "REQUEST_WITHIN_DEFENSIVE_SCOPE"),
            SecurityClass.ACTIVE: ("rejected", "CAPABILITY_ACTIVE_CONTRACT_REQUIRED"),
            SecurityClass.PROHIBITED: ("rejected", "CAPABILITY_PROHIBITED"),
        }[definition.security_class]
        cases.append(({**request, "capabilities": [name]}, state, code))
    cases.append(({**request, "authorization_confirmed": False, "capabilities": ["unknown", "obtain_credentials", "run_authorized_lab_scan"]}, "rejected", "CAPABILITY_PROHIBITED"))
    for value, decision, code in cases:
        before = deepcopy(value)
        result = core.assess_request(value, now_utc=NOW)
        _require(result["decision"] == decision and code in result["reason_codes"], f"policy case failed: {code}")
        _require(result == core.assess_request(value, now_utc=NOW), "policy is not deterministic")
        _require(before == value, "policy changed caller input")
        _require(len(result["reasons"]) == len(result["reason_codes"]), "reason/code pairing changed")
    active = {**request, "target_scope": "isolated_local_lab", "capabilities": ["run_authorized_lab_scan"]}
    raw = {
        "contract_id": "12345678-1234-5678-9234-567812345678",
        "contract_type": "authorized_lab_scan_v1", "capability": "run_authorized_lab_scan",
        "target_scope": "isolated_local_lab",
        "target": {"kind": "ip_literal", "address": "127.0.0.1", "ports": [443], "transport": "tcp"},
        "validity": {"not_before_utc": "2026-10-01T11:59:00Z", "expires_at_utc": "2026-10-01T12:05:00Z"},
        "limits": {"max_connections": 1, "max_concurrency": 1, "max_runtime_seconds": 1, "max_bytes_sent": 0},
        "allowed_actions": ["tcp_connect_probe"], "approval_points": ["before_execution"],
        "abort_conditions": ["authorization_revoked", "deadline_reached", "host_policy_denied", "resource_limit_reached", "target_mismatch"],
    }
    contract = validate_execution_contract(raw, bound_request=active, now_utc=NOW)
    _require(core.assess_request(active, validated_contract=contract, now_utc=NOW)["decision"] == "approved", "valid bound active contract rejected")
    for value, supplied, moment in (
        (active, raw, NOW), (active, contract, NOW + timedelta(minutes=6)),
        (active, contract, NOW - timedelta(minutes=2)),
        ({**active, "purpose": "changed request"}, contract, NOW),
        ({**active, "authorization_confirmed": False}, contract, NOW),
    ):
        _require(core.assess_request(value, validated_contract=supplied, now_utc=moment)["decision"] == "rejected", "unbound or invalid active contract approved")


def _service_order_cases(service):
    request = {"purpose": "synthetic"}
    path = Path("synthetic-audit.jsonl")
    for decision in ("approved", "rejected", "needs_clarification"):
        for failure_at in (None, "ready", "policy", "append"):
            events = []
            assessment = {"decision": decision}

            def event(name):
                events.append(name)
                if name == failure_at:
                    raise OSError("synthetic boundary failure")

            def ready(actual):
                _require(actual == path, "readiness path changed")
                event("ready")

            def policy(value, **kwargs):
                _require(value is request and kwargs["now_utc"] == NOW, "policy input changed")
                event("policy")
                return assessment

            def append(actual, value, result, **kwargs):
                _require(actual == path and value is request and result is assessment, "audit binding changed")
                event("append")

            with patch.object(service, "ensure_audit_log_ready", ready), patch.object(service, "_assess_request", policy), patch.object(service, "append_audit_assessment", append):
                try:
                    result = service.assess_and_record_request(request, audit_path=path, now_utc=NOW)
                except OSError:
                    _require(failure_at is not None, "unexpected release failure")
                else:
                    _require(failure_at is None and result is assessment, "failed audit released result")
            expected = ["ready", "policy", "append"]
            if failure_at is not None:
                expected = expected[:expected.index(failure_at) + 1]
            _require(events == expected, "decision service reordered readiness/policy/audit")
