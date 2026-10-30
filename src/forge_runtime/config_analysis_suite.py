"""Fixed verifier for fail-closed redaction and local config analysis."""

import ast
from copy import deepcopy
from typing import Mapping


_CONFIG_ANALYZER_TEMPLATE = '''"""Pure, bounded analysis of synthetic application configuration data."""

EXPECTED_FIELDS = frozenset(
    {
        "debug",
        "tls_enabled",
        "logging_enabled",
        "allowed_hosts",
        "admin_interface_public",
    }
)
OPTIONAL_REDACTED_FIELDS = frozenset(
    {"api_token", "database_password", "private_key"}
)
REDACTION_MARKER = "REDACTED"
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "private_key", "api_key")
MAX_ALLOWED_HOSTS = 20
MAX_HOST_LENGTH = 253


def analyze_application_config(config: object) -> dict[str, object]:
    """Analyze a small in-memory configuration without performing any I/O."""
    redaction_reasons = _validate_redaction_contract(config)
    if redaction_reasons:
        return {
            "decision": "rejected",
            "findings": [],
            "reasons": redaction_reasons,
        }

    validation_reasons = _validate_config(config)
    if validation_reasons:
        return {
            "decision": "needs_clarification",
            "findings": [],
            "reasons": validation_reasons,
        }

    assert type(config) is dict
    findings: list[dict[str, str]] = []
    if config["debug"] is True:
        findings.append(
            _finding(
                "debug_enabled",
                "medium",
                "debug",
                "Debug mode is enabled.",
                "Disable debug mode outside an isolated development environment.",
            )
        )
    if config["tls_enabled"] is False:
        findings.append(
            _finding(
                "tls_disabled",
                "high",
                "tls_enabled",
                "TLS is disabled.",
                "Enable TLS before transmitting data over a network.",
            )
        )
    if config["logging_enabled"] is False:
        findings.append(
            _finding(
                "security_logging_disabled",
                "medium",
                "logging_enabled",
                "Security-relevant logging is disabled.",
                "Enable appropriate security logging without recording secrets.",
            )
        )
    if "*" in config["allowed_hosts"]:
        findings.append(
            _finding(
                "wildcard_allowed_host",
                "high",
                "allowed_hosts",
                "The allowed host list contains a wildcard.",
                "Replace the wildcard with the exact required host names.",
            )
        )
    if config["admin_interface_public"] is True:
        findings.append(
            _finding(
                "public_admin_interface",
                "high",
                "admin_interface_public",
                "The administration interface is marked as public.",
                "Restrict the administration interface to an authorized network.",
            )
        )

    return {
        "decision": "completed",
        "findings": findings,
        "reasons": ["analysis is limited to the fixed synthetic configuration rules"],
    }


def _validate_config(config: object) -> list[str]:
    if type(config) is not dict:
        return ["config must be a dictionary"]

    reasons: list[str] = []
    keys = list(config.keys())
    non_string_keys = [key for key in keys if type(key) is not str]
    if non_string_keys:
        reasons.append("all config field names must be strings")

    string_keys = [key for key in keys if type(key) is str]
    unknown_sensitive_keys = sorted(
        key
        for key in string_keys
        if key not in OPTIONAL_REDACTED_FIELDS
        if any(part in key.casefold() for part in SENSITIVE_KEY_PARTS)
    )
    if unknown_sensitive_keys:
        reasons.extend(
            f"sensitive field must be removed before analysis: {key}"
            for key in unknown_sensitive_keys
        )

    actual_fields = set(string_keys)
    missing = sorted(EXPECTED_FIELDS - actual_fields)
    unknown = sorted(actual_fields - EXPECTED_FIELDS - OPTIONAL_REDACTED_FIELDS)
    reasons.extend(f"missing field: {field}" for field in missing)
    reasons.extend(f"unknown field: {field}" for field in unknown)

    for field in ("debug", "tls_enabled", "logging_enabled", "admin_interface_public"):
        if field in config and type(config[field]) is not bool:
            reasons.append(f"{field} must be a boolean")

    if "allowed_hosts" in config:
        allowed_hosts = config["allowed_hosts"]
        if type(allowed_hosts) is not list:
            reasons.append("allowed_hosts must be a list")
        elif len(allowed_hosts) > MAX_ALLOWED_HOSTS:
            reasons.append(f"allowed_hosts must contain at most {MAX_ALLOWED_HOSTS} items")
        else:
            for index, host in enumerate(allowed_hosts):
                if type(host) is not str or not host.strip():
                    reasons.append(
                        f"allowed_hosts item at index {index} must be a non-empty string"
                    )
                elif len(host) > MAX_HOST_LENGTH:
                    reasons.append(
                        f"allowed_hosts item at index {index} exceeds {MAX_HOST_LENGTH} characters"
                    )
    return reasons


def _validate_redaction_contract(config: object) -> list[str]:
    if type(config) is not dict:
        return []
    violations = sorted(
        field
        for field in OPTIONAL_REDACTED_FIELDS
        if field in config and config[field] != REDACTION_MARKER
    )
    return [
        f"sensitive field must contain exact REDACTED marker: {field}"
        for field in violations
    ]


def _finding(
    rule_id: str,
    severity: str,
    setting: str,
    message: str,
    recommendation: str,
) -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "setting": setting,
        "message": message,
        "recommendation": recommendation,
    }
'''


def run_redacted_config_analysis(contents: Mapping[str, str]) -> str | None:
    """Validate a closed safe AST, then execute only its pure functions."""
    try:
        tree = ast.parse(
            contents["agent/config_analyzer.py"],
            filename="agent/config_analyzer.py",
        )
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"

    structure_failure = _validate_safe_structure(tree)
    if structure_failure is not None:
        return structure_failure
    return _run_analysis_cases(tree)


def _validate_safe_structure(tree: ast.Module) -> str | None:
    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    assignments = body[:6]
    functions = body[6:]
    if (
        len(body) != 10
        or any(type(node) is not ast.Assign for node in assignments)
        or any(type(node) is not ast.FunctionDef for node in functions)
    ):
        return "config analyzer may contain only six constants and four functions"

    expected_constants = (
        (
            "EXPECTED_FIELDS",
            frozenset(
                {
                    "debug",
                    "tls_enabled",
                    "logging_enabled",
                    "allowed_hosts",
                    "admin_interface_public",
                }
            ),
        ),
        (
            "OPTIONAL_REDACTED_FIELDS",
            frozenset({"api_token", "database_password", "private_key"}),
        ),
        ("REDACTION_MARKER", "REDACTED"),
        (
            "SENSITIVE_KEY_PARTS",
            ("password", "secret", "token", "private_key", "api_key"),
        ),
        ("MAX_ALLOWED_HOSTS", 20),
        ("MAX_HOST_LENGTH", 253),
    )
    for node, (name, expected) in zip(assignments, expected_constants):
        assert type(node) is ast.Assign
        if (
            len(node.targets) != 1
            or type(node.targets[0]) is not ast.Name
            or node.targets[0].id != name
        ):
            return "config analyzer constants do not match the fixed contract"
        actual = _safe_constant_value(node.value)
        if type(actual) is not type(expected) or actual != expected:
            return "config analyzer constants do not match the fixed contract"

    expected_functions = (
        ("analyze_application_config", ("config",)),
        ("_validate_config", ("config",)),
        ("_validate_redaction_contract", ("config",)),
        (
            "_finding",
            ("rule_id", "severity", "setting", "message", "recommendation"),
        ),
    )
    for node, (name, arguments) in zip(functions, expected_functions):
        assert type(node) is ast.FunctionDef
        if not _has_safe_signature(node, name, arguments):
            return "config analyzer function signatures do not match the contract"
        failure = _validate_function_ast(node)
        if failure is not None:
            return failure
    return None


def _is_docstring(node: ast.AST) -> bool:
    return (
        type(node) is ast.Expr
        and type(node.value) is ast.Constant
        and type(node.value.value) is str
    )


def _safe_constant_value(node: ast.expr) -> object:
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
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return object()


def _has_safe_signature(
    node: ast.FunctionDef,
    name: str,
    arguments: tuple[str, ...],
) -> bool:
    return (
        node.name == name
        and not node.decorator_list
        and node.type_comment is None
        and not node.args.posonlyargs
        and node.args.vararg is None
        and not node.args.kwonlyargs
        and not node.args.kw_defaults
        and node.args.kwarg is None
        and not node.args.defaults
        and tuple(argument.arg for argument in node.args.args) == arguments
    )


def _validate_function_ast(function: ast.FunctionDef) -> str | None:
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
        ast.Raise,
        ast.Try,
        ast.While,
        ast.With,
        ast.Yield,
        ast.YieldFrom,
    )
    allowed_named_calls = {
        "analyze_application_config": {
            "_finding",
            "_validate_config",
            "_validate_redaction_contract",
            "isinstance",
            "type",
        },
        "_validate_config": {
            "any",
            "enumerate",
            "isinstance",
            "len",
            "list",
            "set",
            "sorted",
            "type",
        },
        "_validate_redaction_contract": {"isinstance", "sorted", "type"},
        "_finding": set(),
    }[function.name]
    for node in ast.walk(function):
        if node is not function and isinstance(node, ast.FunctionDef):
            return "config analyzer may not define nested functions"
        if isinstance(node, forbidden):
            return "config analyzer contains a forbidden control or access feature"
        if type(node) is ast.Assign and not all(
            _is_name_target(target) for target in node.targets
        ):
            return "config analyzer may assign only local names"
        if type(node) is ast.AnnAssign and not _is_name_target(node.target):
            return "config analyzer may assign only local names"
        if type(node) is ast.AugAssign:
            return "config analyzer may not use augmented assignment"
        if type(node) is ast.For and not _is_loop_target(node.target):
            return "config analyzer loop target is not allowed"
        if type(node) is ast.BinOp and type(node.op) not in (ast.Sub, ast.BitOr):
            return "config analyzer contains an unsupported binary operation"
        if type(node) is ast.Call:
            failure = _validate_call(node, allowed_named_calls)
            if failure is not None:
                return failure
        if type(node) is ast.Attribute and node.attr.startswith("_"):
            return "config analyzer contains a private attribute access"
    return None


def _is_name_target(node: ast.expr) -> bool:
    return type(node) is ast.Name


def _is_loop_target(node: ast.expr) -> bool:
    if type(node) is ast.Name:
        return True
    return type(node) is ast.Tuple and all(
        type(element) is ast.Name for element in node.elts
    )


def _validate_call(node: ast.Call, allowed_named_calls: set[str]) -> str | None:
    if node.keywords:
        return "config analyzer calls may not use keyword arguments"
    if type(node.func) is ast.Name:
        if node.func.id not in allowed_named_calls:
            return f"config analyzer call is not allowed: {node.func.id}"
        return None
    if type(node.func) is not ast.Attribute:
        return "config analyzer contains an indirect call"
    receiver = node.func.value
    attribute = node.func.attr
    if (
        attribute in {"append", "extend"}
        and type(receiver) is ast.Name
        and receiver.id
        in {
            "errors",
            "findings",
            "reasons",
            "redaction_errors",
            "validation_errors",
        }
    ):
        return None
    if (
        attribute in {"get", "keys"}
        and type(receiver) is ast.Name
        and receiver.id == "config"
    ):
        return None
    if (
        attribute in {"casefold", "lower", "strip"}
        and type(receiver) is ast.Name
    ):
        return None
    if (
        attribute == "issubset"
        and type(receiver) is ast.Name
    ):
        return None
    return f"config analyzer method call is not allowed: {attribute}"


def _load_validated_analysis(tree: ast.Module):
    failure = _validate_safe_structure(tree)
    if failure is not None:
        raise ValueError(failure)
    functions = [node for node in tree.body if type(node) is ast.FunctionDef]
    if [node.name for node in functions] != [
        "analyze_application_config",
        "_validate_config",
        "_validate_redaction_contract",
        "_finding",
    ]:
        raise ValueError("config analyzer functions are missing")

    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    safe_globals = {
        "__builtins__": {
            "any": any,
            "bool": bool,
            "dict": dict,
            "enumerate": enumerate,
            "isinstance": isinstance,
            "len": len,
            "list": list,
            "object": object,
            "repr": repr,
            "set": set,
            "sorted": sorted,
            "str": str,
            "type": type,
        },
        "EXPECTED_FIELDS": frozenset(
            {
                "debug",
                "tls_enabled",
                "logging_enabled",
                "allowed_hosts",
                "admin_interface_public",
            }
        ),
        "OPTIONAL_REDACTED_FIELDS": frozenset(
            {"api_token", "database_password", "private_key"}
        ),
        "REDACTION_MARKER": "REDACTED",
        "SENSITIVE_KEY_PARTS": (
            "password",
            "secret",
            "token",
            "private_key",
            "api_key",
        ),
        "MAX_ALLOWED_HOSTS": 20,
        "MAX_HOST_LENGTH": 253,
    }
    exec(compile(module, "<forge-config-analysis-suite>", "exec"), safe_globals)
    return safe_globals


def _run_analysis_cases(tree: ast.Module) -> str | None:
    safe_globals = _load_validated_analysis(tree)
    analyze = safe_globals["analyze_application_config"]
    safe = {
        "debug": False,
        "tls_enabled": True,
        "logging_enabled": True,
        "allowed_hosts": ["app.test"],
        "admin_interface_public": False,
    }
    expected_safe = {
        "decision": "completed",
        "findings": [],
        "reasons": ["analysis is limited to the fixed synthetic configuration rules"],
    }
    insecure = {
        "debug": True,
        "tls_enabled": False,
        "logging_enabled": False,
        "allowed_hosts": ["*"],
        "admin_interface_public": True,
    }

    class ConfigSubclass(dict):
        pass

    cases: tuple[tuple[str, object, str, list[str]], ...] = (
        ("safe", safe, "completed", []),
        ("all_findings", insecure, "completed", [
            "debug_enabled",
            "tls_disabled",
            "security_logging_disabled",
            "wildcard_allowed_host",
            "public_admin_interface",
        ]),
        ("wrong_container", "not a dictionary", "needs_clarification", []),
        ("exact_dict", ConfigSubclass(safe), "needs_clarification", []),
        ("missing_fields", {}, "needs_clarification", []),
        ("unknown_field", dict(safe, extra=True), "needs_clarification", []),
        ("non_string_key", dict(safe, **{"extra": True}) | {1: True}, "needs_clarification", []),
        ("exact_boolean", dict(safe, debug=1), "needs_clarification", []),
        ("host_count", dict(safe, allowed_hosts=["host"] * 21), "needs_clarification", []),
        ("host_length", dict(safe, allowed_hosts=["a" * 254]), "needs_clarification", []),
        ("blank_host", dict(safe, allowed_hosts=["  "]), "needs_clarification", []),
        ("casefold_secret", dict(safe, Service_Secret="REDACTED"), "needs_clarification", []),
        ("valid_redaction", dict(safe, api_token="REDACTED"), "completed", []),
        ("redaction_first", dict(insecure, api_token="actual-secret"), "rejected", []),
    )
    failures: list[str] = []
    for label, config, decision, rule_ids in cases:
        original = deepcopy(config)
        try:
            result = analyze(config)
            repeated = analyze(config)
        except Exception as exc:
            exception_failure = {
                "non_string_key": (
                    "non-string field caused an exception; after recording the "
                    "field-name error, return safely or derive later sets only "
                    "from string keys"
                ),
            }.get(label, f"{label} raised {type(exc).__name__}")
            failures.append(exception_failure)
            continue
        if result != repeated or config != original:
            failures.append(f"{label} was nondeterministic or mutated its input")
            continue
        if type(result) is not dict or set(result) != {
            "decision",
            "findings",
            "reasons",
        }:
            failures.append(f"{label} returned an invalid result schema")
            continue
        if result["decision"] != decision:
            decision_failure = {
                "exact_dict": (
                    "dict subclass was accepted; both validators must use "
                    "type(config) is dict rather than isinstance"
                ),
                "blank_host": (
                    "whitespace-only host was accepted; validate with "
                    "not host.strip()"
                ),
                "casefold_secret": (
                    "secret-like field was missed; match field.casefold()"
                ),
            }.get(label, f"{label} returned an unexpected decision")
            failures.append(decision_failure)
            continue
        if type(result["findings"]) is not list or type(result["reasons"]) is not list:
            failures.append(f"{label} returned invalid findings or reasons")
            continue
        actual_rule_ids = [finding.get("rule_id") for finding in result["findings"]]
        if actual_rule_ids != rule_ids:
            failures.append(f"{label} returned unexpected or unordered findings")
            continue
        if "actual-secret" in repr(result):
            failures.append(f"{label} exposed a sensitive value")
    if analyze(safe) != expected_safe:
        failures.append("safe result does not match the fixed contract")
    failures.extend(_check_contract_details(analyze, safe, insecure))
    if failures:
        return "config analyzer behavior failed: " + "; ".join(failures)
    return None


def _check_contract_details(analyze, safe: dict, insecure: dict) -> list[str]:
    failures: list[str] = []
    expected_findings = [
        {
            "rule_id": "debug_enabled",
            "severity": "medium",
            "setting": "debug",
            "message": "Debug mode is enabled.",
            "recommendation": (
                "Disable debug mode outside an isolated development environment."
            ),
        },
        {
            "rule_id": "tls_disabled",
            "severity": "high",
            "setting": "tls_enabled",
            "message": "TLS is disabled.",
            "recommendation": "Enable TLS before transmitting data over a network.",
        },
        {
            "rule_id": "security_logging_disabled",
            "severity": "medium",
            "setting": "logging_enabled",
            "message": "Security-relevant logging is disabled.",
            "recommendation": (
                "Enable appropriate security logging without recording secrets."
            ),
        },
        {
            "rule_id": "wildcard_allowed_host",
            "severity": "high",
            "setting": "allowed_hosts",
            "message": "The allowed host list contains a wildcard.",
            "recommendation": (
                "Replace the wildcard with the exact required host names."
            ),
        },
        {
            "rule_id": "public_admin_interface",
            "severity": "high",
            "setting": "admin_interface_public",
            "message": "The administration interface is marked as public.",
            "recommendation": (
                "Restrict the administration interface to an authorized network."
            ),
        },
    ]
    if analyze(insecure).get("findings") != expected_findings:
        failures.append("finding records or fixed text differ")

    sensitive_result = analyze(dict(safe, Service_Secret="REDACTED"))
    expected_sensitive_reason = (
        "sensitive field must be removed before analysis: Service_Secret"
    )
    if expected_sensitive_reason not in sensitive_result.get("reasons", []):
        failures.append("secret-like field matching is not case-insensitive")

    for field in (
        "debug",
        "tls_enabled",
        "logging_enabled",
        "allowed_hosts",
        "admin_interface_public",
    ):
        config = dict(safe)
        del config[field]
        result = analyze(config)
        if f"missing field: {field}" not in result.get("reasons", []):
            failures.append(f"missing-field reason differs for {field}")

    for field in ("api_token", "database_password", "private_key"):
        accepted = analyze(dict(safe, **{field: "REDACTED"}))
        rejected = analyze(dict(insecure, **{field: "secret-123"}))
        if accepted.get("decision") != "completed":
            failures.append(f"exact marker was not accepted for {field}")
        if (
            rejected.get("decision") != "rejected"
            or rejected.get("findings") != []
            or f"sensitive field must contain exact REDACTED marker: {field}"
            not in rejected.get("reasons", [])
            or "secret-123" in repr(rejected)
        ):
            failures.append(f"fail-closed redaction differs for {field}")
    return failures
