"""Fixed host verifier for the audited passive configuration-analysis gate."""

import ast
from typing import Mapping


_EXPECTED_SOURCE = '''from .config_analyzer import analyze_application_config
from .decision_service import assess_and_record_request

def run_audited_config_analysis(request, config, audit_path):
    assessment = assess_and_record_request(request, audit_path=audit_path)
    if assessment["decision"] != "approved":
        return assessment, None
    if type(request) is not dict or request.get("capabilities") != ["review_user_provided_configuration"]:
        raise ValueError("request is not bound to the configuration analysis capability")
    analysis = analyze_application_config(config)
    return assessment, analysis
'''
_EXPECTED_TREE = ast.parse(_EXPECTED_SOURCE)


def run_audited_config_analysis(contents: Mapping[str, str]) -> str | None:
    """Accept only the fixed gate, then verify release ordering with safe stubs."""
    try:
        tree = ast.parse(
            contents["agent/config_analysis_service.py"],
            filename="agent/config_analysis_service.py",
        )
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"
    if ast.dump(tree, include_attributes=False) != ast.dump(
        _EXPECTED_TREE, include_attributes=False
    ):
        return "configuration facade must use the exact audited release structure"

    function = tree.body[-1]
    assert type(function) is ast.FunctionDef
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    events: list[str] = []

    def assess_and_record_request(request: object, *, audit_path: object):
        events.append("audit")
        assert audit_path == "audit.jsonl"
        assert type(request) is dict
        if request.get("audit_failure") is True:
            raise OSError("synthetic durable audit failure")
        if request.get("audit_failure") == "integrity":
            raise ValueError("synthetic audit integrity failure")
        return request["assessment"]

    def analyze_application_config(config: object):
        events.append("analyzer")
        assert config is sample_config
        return analysis_result

    safe_globals = {
        "__builtins__": {"type": type, "dict": dict, "ValueError": ValueError},
        "assess_and_record_request": assess_and_record_request,
        "analyze_application_config": analyze_application_config,
    }
    exec(compile(module, "<forge-audited-config-suite>", "exec"), safe_globals)
    gate = safe_globals["run_audited_config_analysis"]
    approved = {"decision": "approved"}
    denied = {"decision": "rejected"}
    sample_config = {"api_key": "synthetic-secret"}
    analysis_result = {"findings": []}
    capability = ["review_user_provided_configuration"]

    try:
        gate(
            {"assessment": approved, "capabilities": capability, "audit_failure": True},
            sample_config,
            "audit.jsonl",
        )
    except OSError:
        pass
    except Exception as exc:
        return f"audit failure raised unexpected {type(exc).__name__}"
    else:
        return "audit failure was swallowed"
    if events != ["audit"]:
        return "audit failure did not prevent analyzer call"

    events.clear()
    try:
        gate(
            {"assessment": approved, "capabilities": capability, "audit_failure": "integrity"},
            sample_config,
            "audit.jsonl",
        )
    except ValueError:
        pass
    else:
        return "audit integrity failure was swallowed"
    if events != ["audit"]:
        return "audit integrity failure did not prevent analyzer call"

    events.clear()
    result = gate(
        {"assessment": denied, "capabilities": capability}, sample_config, "audit.jsonl"
    )
    if events != ["audit"] or result != (denied, None):
        return "denied request was not audited or reached analyzer"

    events.clear()
    result = gate(
        {"assessment": approved, "capabilities": capability}, sample_config, "audit.jsonl"
    )
    if events != ["audit", "analyzer"] or result != (approved, analysis_result):
        return "approved request did not follow audit then analyzer order"

    events.clear()
    try:
        gate(
            {"assessment": approved, "capabilities": ["other"]},
            sample_config,
            "audit.jsonl",
        )
    except ValueError:
        pass
    else:
        return "wrong capability was accepted"
    if events != ["audit"]:
        return "wrong capability reached analyzer"
    return None
