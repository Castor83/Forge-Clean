"""Fixed verifier for a minimized, tamper-evident in-memory audit chain."""

import ast
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Mapping
from uuid import UUID


_ENTRY_FIELDS = frozenset(
    {
        "event_id",
        "timestamp_utc",
        "policy_version",
        "decision_code",
        "reason_codes",
        "request_hash",
        "previous_hash",
        "entry_hash",
    }
)
_DECISION_CODES = frozenset(
    {"REQUEST_APPROVED", "REQUEST_NEEDS_CLARIFICATION", "REQUEST_REJECTED"}
)
_REASON_CODES = frozenset(
    {
        "REQUEST_TYPE_INVALID",
        "FIELD_NAME_INVALID",
        "FIELD_MISSING",
        "FIELD_UNKNOWN",
        "PURPOSE_INVALID",
        "TARGET_SCOPE_INVALID",
        "AUTHORIZATION_REQUIRED",
        "CAPABILITIES_INVALID",
        "CAPABILITY_ITEM_INVALID",
        "CAPABILITY_DUPLICATE",
        "CAPABILITY_PROHIBITED",
        "CAPABILITY_ACTIVE_CONTRACT_REQUIRED",
        "CAPABILITY_ACTIVE_CONTRACT_VALIDATED",
        "CAPABILITY_UNKNOWN",
        "REQUEST_WITHIN_DEFENSIVE_SCOPE",
    }
)
_EXPECTED_FUNCTIONS = (
    ("create_audit_entry", ("request", "assessment", "event_id", "timestamp_utc", "policy_version", "previous_hash")),
    ("verify_audit_chain", ("entries",)),
    ("_validate_entry", ("entry", "expected_previous")),
    ("_validate_metadata", ("event_id", "timestamp_utc", "policy_version", "previous_hash")),
    ("_require_hash", ("value", "field")),
    ("_hash_json", ("value",)),
    ("_canonical_json", ("value",)),
)


def run_minimized_audit_chain(contents: Mapping[str, str]) -> str | None:
    """Validate a closed AST, then exercise the audit functions with host data."""
    try:
        tree = ast.parse(contents["agent/audit_chain.py"], filename="agent/audit_chain.py")
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"
    failure = _validate_module(tree)
    if failure is not None:
        return failure
    return _run_audit_cases(tree)


def _validate_module(tree: ast.Module) -> str | None:
    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    imports = body[:4]
    assignments = body[4:9]
    functions = body[9:]
    if len(body) != 16:
        return "audit module may contain only four imports, five constants, and seven functions"
    if not _imports_match(imports):
        return "audit imports do not match the fixed standard-library contract"
    failure = _validate_constants(assignments)
    if failure is not None:
        return failure
    if any(type(node) is not ast.FunctionDef for node in functions):
        return "audit module contains unexpected top-level code"
    for node, (name, arguments) in zip(functions, _EXPECTED_FUNCTIONS):
        assert type(node) is ast.FunctionDef
        if not _signature_matches(node, name, arguments):
            return "audit function signatures do not match the fixed contract"
        failure = _validate_function(node)
        if failure is not None:
            return failure
        if name == "create_audit_entry" and not _has_request_hash_assignment(node):
            return (
                "replace the request_hash assignment with the exact line: "
                "request_hash = _hash_json(request)"
            )
    return None


def _has_request_hash_assignment(function: ast.FunctionDef) -> bool:
    for node in function.body:
        if (
            type(node) is ast.Assign
            and len(node.targets) == 1
            and type(node.targets[0]) is ast.Name
            and node.targets[0].id == "request_hash"
        ):
            return (
                type(node.value) is ast.Call
                and type(node.value.func) is ast.Name
                and node.value.func.id == "_hash_json"
                and len(node.value.args) == 1
                and type(node.value.args[0]) is ast.Name
                and node.value.args[0].id == "request"
                and not node.value.keywords
            )
    return False


def _imports_match(nodes: list[ast.stmt]) -> bool:
    if len(nodes) != 4:
        return False
    expected = (
        (ast.Import, None, (("hashlib", None),)),
        (ast.Import, None, (("json", None),)),
        (ast.ImportFrom, "datetime", (("datetime", None), ("timezone", None))),
        (ast.ImportFrom, "uuid", (("UUID", None),)),
    )
    for node, (node_type, module, names) in zip(nodes, expected):
        if type(node) is not node_type:
            return False
        if type(node) is ast.ImportFrom and (node.module != module or node.level != 0):
            return False
        if tuple((alias.name, alias.asname) for alias in node.names) != names:
            return False
    return True


def _validate_constants(nodes: list[ast.stmt]) -> str | None:
    expected = (
        ("GENESIS_HASH", "0" * 64),
        ("ENTRY_FIELDS", _ENTRY_FIELDS),
        ("DECISION_CODES", _DECISION_CODES),
        ("REASON_CODES", _REASON_CODES),
        ("MAX_REASON_CODES", 20),
    )
    if len(nodes) != len(expected) or any(type(node) is not ast.Assign for node in nodes):
        return "audit constants do not match the fixed contract"
    for node, (name, expected_value) in zip(nodes, expected):
        assert type(node) is ast.Assign
        if (
            len(node.targets) != 1
            or type(node.targets[0]) is not ast.Name
            or node.targets[0].id != name
        ):
            return f"audit constant must use the exact name and order: {name}"
        actual = _constant_value(node.value)
        if type(actual) is not type(expected_value) or actual != expected_value:
            expected_kind = "frozenset" if type(expected_value) is frozenset else type(expected_value).__name__
            return f"audit constant {name} must use the exact {expected_kind} value from SPEC.md"
    return None


def _constant_value(node: ast.expr) -> object:
    if (
        type(node) is ast.Call
        and type(node.func) is ast.Name
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        try:
            return frozenset(ast.literal_eval(node.args[0]))
        except (TypeError, ValueError):
            return object()
    if (
        type(node) is ast.BinOp
        and type(node.op) is ast.Mult
        and type(node.left) is ast.Constant
        and node.left.value == "0"
        and type(node.right) is ast.Constant
        and node.right.value == 64
    ):
        return "0" * 64
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return object()


def _signature_matches(node: ast.FunctionDef, name: str, arguments: tuple[str, ...]) -> bool:
    defaults = node.args.defaults
    default_ok = (
        name == "create_audit_entry"
        and len(defaults) == 1
        and type(defaults[0]) is ast.Name
        and defaults[0].id == "GENESIS_HASH"
    ) or (name != "create_audit_entry" and not defaults)
    return (
        node.name == name
        and not node.decorator_list
        and node.type_comment is None
        and not node.args.posonlyargs
        and node.args.vararg is None
        and not node.args.kwonlyargs
        and not node.args.kw_defaults
        and node.args.kwarg is None
        and tuple(argument.arg for argument in node.args.args) == arguments
        and default_ok
    )


def _validate_function(function: ast.FunctionDef) -> str | None:
    forbidden = (
        ast.AsyncFunctionDef,
        ast.Await,
        ast.ClassDef,
        ast.Delete,
        ast.Global,
        ast.Import,
        ast.ImportFrom,
        ast.Lambda,
        ast.Match,
        ast.NamedExpr,
        ast.Nonlocal,
        ast.While,
        ast.With,
        ast.Yield,
        ast.YieldFrom,
    )
    allowed_calls = {
        "create_audit_entry": {"ValueError", "_hash_json", "_require_hash", "_validate_metadata", "any", "isinstance", "len", "list", "type"},
        "verify_audit_chain": {"ValueError", "_validate_entry", "enumerate", "isinstance", "len", "type"},
        "_validate_entry": {"ValueError", "_hash_json", "_require_hash", "_validate_metadata", "any", "isinstance", "len", "list", "set", "type"},
        "_validate_metadata": {"UUID", "ValueError", "_require_hash", "all", "isinstance", "len", "str", "type"},
        "_require_hash": {"ValueError", "all", "any", "isinstance", "len", "type"},
        "_hash_json": {"_canonical_json"},
        "_canonical_json": set(),
    }[function.name]
    for node in ast.walk(function):
        if node is not function and isinstance(node, ast.FunctionDef):
            return "audit module may not define nested functions"
        if isinstance(node, forbidden):
            return "audit module contains a forbidden control or access feature"
        if type(node) is ast.Assign and not all(_is_safe_assignment_target(target) for target in node.targets):
            return "audit functions may assign only local names"
        if type(node) is ast.AnnAssign and type(node.target) is not ast.Name:
            return "audit functions may assign only local names"
        if type(node) is ast.AugAssign:
            return "audit module may not use augmented assignment"
        if type(node) is ast.Call:
            failure = _validate_call(node, allowed_calls)
            if failure is not None:
                return failure
        if type(node) is ast.Attribute and node.attr.startswith("_"):
            return "audit module contains a private attribute access"
        if type(node) is ast.Raise and not _is_value_error_raise(node):
            return "audit module may raise only ValueError"
        if type(node) is ast.Try and (
            node.orelse
            or node.finalbody
            or any(
                handler.name is not None
                or type(handler.type) is not ast.Name
                or handler.type.id not in {"Exception", "ValueError"}
                for handler in node.handlers
            )
        ):
            return "audit exception handling does not match the safe contract"
    return None


def _is_safe_assignment_target(node: ast.expr) -> bool:
    if type(node) is ast.Name:
        return True
    return (
        type(node) is ast.Subscript
        and type(node.value) is ast.Name
        and node.value.id == "entry_data"
        and type(node.slice) is ast.Constant
        and node.slice.value == "entry_hash"
    )


def _validate_call(node: ast.Call, allowed_calls: set[str]) -> str | None:
    if type(node.func) is ast.Name:
        if node.func.id not in allowed_calls:
            return f"audit call is not allowed: {node.func.id}"
        return None
    if type(node.func) is not ast.Attribute:
        return "audit module contains an indirect call"
    receiver = node.func.value
    attribute = node.func.attr
    if attribute == "get" and type(receiver) is ast.Name and receiver.id in {"assessment", "entry"}:
        return None
    if attribute == "keys" and type(receiver) is ast.Name and receiver.id == "entry":
        return None
    if attribute == "issubset" and type(receiver) is ast.Name and receiver.id == "ENTRY_FIELDS":
        return None
    if attribute in {"endswith", "strip"} and type(receiver) is ast.Name:
        return None
    if attribute == "encode" and (
        type(receiver) is ast.Name
        or (
            type(receiver) is ast.Call
            and type(receiver.func) is ast.Name
            and receiver.func.id == "_canonical_json"
        )
    ):
        return None
    if attribute == "fromisoformat" and type(receiver) is ast.Name and receiver.id == "datetime":
        return None
    if attribute == "replace" and type(receiver) is ast.Name and receiver.id == "timestamp_utc":
        return None
    if attribute == "strftime" and type(receiver) is ast.Name:
        return None
    if attribute == "dumps" and type(receiver) is ast.Name and receiver.id == "json":
        expected_keywords = {"ensure_ascii", "allow_nan", "sort_keys", "separators"}
        if {keyword.arg for keyword in node.keywords} == expected_keywords:
            return None
    if attribute == "sha256" and type(receiver) is ast.Name and receiver.id == "hashlib":
        return None
    if attribute == "hexdigest" and type(receiver) is ast.Call:
        return None
    return f"audit method call is not allowed: {attribute}"


def _is_value_error_raise(node: ast.Raise) -> bool:
    return (
        type(node.exc) is ast.Call
        and type(node.exc.func) is ast.Name
        and node.exc.func.id == "ValueError"
        and len(node.exc.args) == 1
        and not node.exc.keywords
    )


def _is_docstring(node: ast.AST) -> bool:
    return type(node) is ast.Expr and type(node.value) is ast.Constant and type(node.value.value) is str


def _run_audit_cases(tree: ast.Module) -> str | None:
    functions = [node for node in tree.body if type(node) is ast.FunctionDef]
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    safe_globals = {
        "__builtins__": {
            "Exception": Exception,
            "ValueError": ValueError,
            "all": all,
            "any": any,
            "bool": bool,
            "dict": dict,
            "enumerate": enumerate,
            "isinstance": isinstance,
            "len": len,
            "list": list,
            "object": object,
            "set": set,
            "str": str,
            "type": type,
        },
        "datetime": datetime,
        "timezone": timezone,
        "UUID": UUID,
        "hashlib": hashlib,
        "json": json,
        "GENESIS_HASH": "0" * 64,
        "ENTRY_FIELDS": _ENTRY_FIELDS,
        "DECISION_CODES": _DECISION_CODES,
        "REASON_CODES": _REASON_CODES,
        "MAX_REASON_CODES": 20,
    }
    exec(compile(module, "<forge-audit-suite>", "exec"), safe_globals)
    create = safe_globals["create_audit_entry"]
    verify = safe_globals["verify_audit_chain"]
    request = {
        "purpose": "private purpose must not be stored",
        "target_scope": "owned_system",
        "authorization_confirmed": True,
        "capabilities": ["review_user_provided_configuration"],
    }
    assessment = {
        "decision_code": "REQUEST_APPROVED",
        "reason_codes": ["REQUEST_WITHIN_DEFENSIVE_SCOPE"],
    }
    original_request = deepcopy(request)
    original_assessment = deepcopy(assessment)
    try:
        first = create(
            request,
            assessment,
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
            "policy-v1",
        )
        repeated = create(
            request,
            assessment,
            "00000000-0000-4000-8000-000000000001",
            "2026-09-26T12:00:00Z",
            "policy-v1",
        )
        second = create(
            request,
            assessment,
            "00000000-0000-4000-8000-000000000002",
            "2026-09-26T12:00:01Z",
            "policy-v1",
            first["entry_hash"],
        )
    except Exception as exc:
        return f"audit creation raised {type(exc).__name__}"
    failures: list[str] = []
    if first != repeated or request != original_request or assessment != original_assessment:
        failures.append("entry creation is nondeterministic or mutates input")
    if (
        first.get("reason_codes") is assessment["reason_codes"]
        or first.get("reason_codes") is repeated.get("reason_codes")
    ):
        failures.append(
            "entry reason_codes is not a fresh list; keep the validated raw "
            "reason_codes variable, then use list(reason_codes) in the unsigned "
            "and returned dictionaries"
        )
    if type(first) is not dict or set(first) != _ENTRY_FIELDS:
        failures.append("entry schema is not minimized and exact")
    serialized = json.dumps(first, sort_keys=True)
    if request["purpose"] in serialized or request["capabilities"][0] in serialized:
        failures.append("entry exposes request content")
    expected_request_hash = hashlib.sha256(
        json.dumps(request, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if first.get("request_hash") != expected_request_hash:
        failures.append("request hash is not canonical SHA-256")
    chain = [first, second]
    original_chain = deepcopy(chain)
    try:
        if verify(chain) is not True:
            failures.append("valid two-entry chain was not accepted")
    except Exception as exc:
        failures.append(f"valid chain raised {type(exc).__name__}")
    if chain != original_chain:
        failures.append("chain verification mutated its input")
    tampered = deepcopy(first)
    tampered["policy_version"] = "tampered"
    changed_decision = dict(first, decision_code="MADE_UP")
    changed_decision["entry_hash"] = _host_entry_hash(changed_decision)
    changed_reason = dict(first, reason_codes=["MADE_UP"])
    changed_reason["entry_hash"] = _host_entry_hash(changed_reason)
    subclass_reason_list = dict(
        first,
        reason_codes=_ListSubclass(first["reason_codes"]),
    )
    subclass_reason_list["entry_hash"] = _host_entry_hash(subclass_reason_list)
    subclass_decision = dict(
        first,
        decision_code=_StringSubclass(first["decision_code"]),
    )
    subclass_decision["entry_hash"] = _host_entry_hash(subclass_decision)
    subclass_reason_item = dict(
        first,
        reason_codes=[_StringSubclass(first["reason_codes"][0])],
    )
    subclass_reason_item["entry_hash"] = _host_entry_hash(subclass_reason_item)
    malformed_request_hash = dict(first, request_hash="g" * 64)
    malformed_request_hash["entry_hash"] = _host_entry_hash(malformed_request_hash)
    subclass_request_hash = dict(
        first,
        request_hash=_StringSubclass(first["request_hash"]),
    )
    subclass_request_hash["entry_hash"] = _host_entry_hash(subclass_request_hash)
    subclass_entry_hash = dict(
        first,
        entry_hash=_StringSubclass(first["entry_hash"]),
    )
    subclass_event_id = dict(
        first,
        event_id=_StringSubclass(first["event_id"]),
    )
    subclass_event_id["entry_hash"] = _host_entry_hash(subclass_event_id)
    subclass_timestamp = dict(
        first,
        timestamp_utc=_StringSubclass(first["timestamp_utc"]),
    )
    subclass_timestamp["entry_hash"] = _host_entry_hash(subclass_timestamp)
    subclass_policy_version = dict(
        first,
        policy_version=_StringSubclass(first["policy_version"]),
    )
    subclass_policy_version["entry_hash"] = _host_entry_hash(subclass_policy_version)
    subclass_previous_hash = dict(
        first,
        previous_hash=_StringSubclass(first["previous_hash"]),
    )
    subclass_previous_hash["entry_hash"] = _host_entry_hash(subclass_previous_hash)
    attack_cases = (
        ("content tampering", [tampered]),
        ("broken ordering", [second, first]),
        (
            "unknown field was accepted; require exact set(entry) == ENTRY_FIELDS",
            [dict(first, extra=True)],
        ),
        ("unregistered decision", [changed_decision]),
        ("unregistered reason", [changed_reason]),
        (
            "list subclass was accepted; require type(entries) is not list",
            _ListSubclass([first]),
        ),
        (
            "entry dict subclass was accepted; require type(entry) is not dict",
            [_DictSubclass(first)],
        ),
        (
            "reason_codes list subclass was accepted; assign the raw entry value, "
            "reject type(reason_codes) is not list, then use list(reason_codes) "
            "only in the unsigned dictionary",
            [subclass_reason_list],
        ),
        (
            "_validate_entry accepted a decision code string subclass; require "
            "type(decision_code) is str there",
            [subclass_decision],
        ),
        (
            "_validate_entry accepted a reason code string subclass; require "
            "type(code) is str there",
            [subclass_reason_item],
        ),
        (
            "_validate_entry accepted a malformed request_hash after rehashing; "
            "call _require_hash on request_hash",
            [malformed_request_hash],
        ),
        (
            "_validate_entry accepted a request_hash string subclass; call "
            "_require_hash on request_hash; in _require_hash replace "
            "'if not isinstance(value, str)' with 'if type(value) is not str'",
            [subclass_request_hash],
        ),
        (
            "_validate_entry accepted an entry_hash string subclass; call "
            "_require_hash on entry_hash; in _require_hash replace "
            "'if not isinstance(value, str)' with 'if type(value) is not str'",
            [subclass_entry_hash],
        ),
        (
            "_validate_metadata accepted an event_id string subclass; require "
            "type(event_id) is str",
            [subclass_event_id],
        ),
        (
            "_validate_metadata accepted a timestamp string subclass; require "
            "type(timestamp_utc) is str",
            [subclass_timestamp],
        ),
        (
            "_validate_metadata accepted a policy version string subclass; require "
            "type(policy_version) is str",
            [subclass_policy_version],
        ),
        (
            "_validate_entry accepted a previous_hash string subclass; its "
            "_validate_metadata call must pass previous_hash, not expected_previous",
            [subclass_previous_hash],
        ),
    )
    for label, entries in attack_cases:
        try:
            verify(entries)
        except ValueError:
            continue
        except Exception as exc:
            failures.append(f"{label} raised {type(exc).__name__} instead of ValueError")
        else:
            failures.append(f"{label} was accepted")
    invalid_creations = (
        ("non-dict request", [], assessment),
        (
            "dict subclass request was accepted; require type(request) is not dict",
            _DictSubclass(request),
            assessment,
        ),
        (
            "dict subclass assessment was accepted; require type(assessment) is not dict",
            request,
            _DictSubclass(assessment),
        ),
        ("unregistered create decision", request, dict(assessment, decision_code="MADE_UP")),
        ("unregistered create reason", request, dict(assessment, reason_codes=["MADE_UP"])),
        (
            "missing create decision_code raised KeyError; in create_audit_entry "
            "replace assessment[\"decision_code\"] with assessment.get(\"decision_code\")",
            request,
            {"reason_codes": list(assessment["reason_codes"])},
        ),
        (
            "missing create reason_codes raised KeyError; in create_audit_entry "
            "replace assessment[\"reason_codes\"] with assessment.get(\"reason_codes\")",
            request,
            {"decision_code": assessment["decision_code"]},
        ),
        (
            "create reason_codes list subclass was accepted; assign the raw assessment value, "
            "reject type(reason_codes) is not list, then use list(reason_codes) "
            "only in the unsigned and returned dictionaries",
            request,
            dict(
                assessment,
                reason_codes=_ListSubclass(assessment["reason_codes"]),
            ),
        ),
        (
            "create decision string subclass was accepted; require type(decision_code) is str",
            request,
            dict(
                assessment,
                decision_code=_StringSubclass(assessment["decision_code"]),
            ),
        ),
        (
            "create reason string subclass was accepted; require type(code) is str",
            request,
            dict(
                assessment,
                reason_codes=[_StringSubclass(assessment["reason_codes"][0])],
            ),
        ),
        (
            "too many reason codes",
            request,
            dict(
                assessment,
                reason_codes=["REQUEST_WITHIN_DEFENSIVE_SCOPE"] * 21,
            ),
        ),
    )
    for label, bad_request, bad_assessment in invalid_creations:
        try:
            create(
                bad_request,
                bad_assessment,
                "00000000-0000-4000-8000-000000000003",
                "2026-09-26T12:00:02Z",
                "policy-v1",
            )
        except ValueError:
            continue
        except Exception as exc:
            failures.append(f"{label} raised {type(exc).__name__} instead of ValueError")
        else:
            failures.append(f"{label} was accepted")
    invalid_metadata = (
        (
            "uppercase UUID was accepted; require type(event_id) is str and str(UUID(event_id)) == event_id",
            "00000000-0000-4000-8000-00000000000A",
            "2026-09-26T12:00:02Z",
            "policy-v1",
            "0" * 64,
        ),
        (
            "timestamp without Z was accepted; require exact UTC whole-second text ending in Z",
            "00000000-0000-4000-8000-000000000003",
            "2026-09-26T12:00:02+00:00",
            "policy-v1",
            "0" * 64,
        ),
        (
            "timestamp without seconds was accepted; require len(timestamp_utc) == 20",
            "00000000-0000-4000-8000-000000000003",
            "2026-09-26T12:00Z",
            "policy-v1",
            "0" * 64,
        ),
        ("fractional timestamp", "00000000-0000-4000-8000-000000000003", "2026-09-26T12:00:02.1Z", "policy-v1", "0" * 64),
        (
            "blank version was accepted; reject whitespace with policy_version.strip()",
            "00000000-0000-4000-8000-000000000003",
            "2026-09-26T12:00:02Z",
            " ",
            "0" * 64,
        ),
        ("invalid previous hash", "00000000-0000-4000-8000-000000000003", "2026-09-26T12:00:02Z", "policy-v1", "A" * 64),
    )
    for label, event_id, timestamp_utc, policy_version, previous_hash in invalid_metadata:
        try:
            create(
                request,
                assessment,
                event_id,
                timestamp_utc,
                policy_version,
                previous_hash,
            )
        except ValueError:
            continue
        except Exception as exc:
            failures.append(f"{label} raised {type(exc).__name__} instead of ValueError")
        else:
            failures.append(f"{label} was accepted")
    if failures:
        return "audit behavior failed: " + "; ".join(failures)
    return None


class _DictSubclass(dict):
    pass


class _ListSubclass(list):
    pass


class _StringSubclass(str):
    pass


def _host_entry_hash(entry: dict[str, object]) -> str:
    unsigned = {key: value for key, value in entry.items() if key != "entry_hash"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
