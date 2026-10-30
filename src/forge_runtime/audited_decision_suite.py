"""Fixed host verifier for policy -> durable audit -> action ordering."""

import ast
from typing import Mapping


_EXPECTED_SOURCE = '''from .audit import append_audit_assessment
from .policy import assess_request
from .socket_action import run_socket_action

def assess_audit_then_execute(request, audit_path):
    assessment = assess_request(request)
    append_audit_assessment(audit_path, request, assessment)
    if assessment["decision"] != "approved":
        return assessment, None
    execution = run_socket_action(request)
    return assessment, execution
'''
_EXPECTED_TREE = ast.parse(_EXPECTED_SOURCE)


def run_audited_decision_gate(contents: Mapping[str, str]) -> str | None:
    """Validate the exact release boundary, then exercise it with safe stubs."""
    try:
        tree = ast.parse(
            contents["agent/decision_gate.py"],
            filename="agent/decision_gate.py",
        )
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"
    if ast.dump(tree, include_attributes=False) != ast.dump(
        _EXPECTED_TREE,
        include_attributes=False,
    ):
        return (
            "decision gate must use the exact policy -> append_audit_assessment -> "
            "decision branch -> socket action -> return structure"
        )
    function = tree.body[-1]
    assert type(function) is ast.FunctionDef
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    events: list[str] = []

    def assess_request(request: object) -> dict[str, object]:
        events.append("policy")
        assert type(request) is dict
        assessment = request["assessment"]
        assert type(assessment) is dict
        return assessment

    def append_audit_assessment(
        audit_path: object,
        request: object,
        assessment: object,
    ) -> None:
        events.append("audit")
        assert audit_path == "audit.jsonl"
        assert type(request) is dict
        assert assessment is request["assessment"]
        if request.get("audit_failure") is True:
            raise OSError("synthetic durable audit failure")

    def run_socket_action(request: object) -> str:
        events.append("socket")
        assert type(request) is dict
        return "socket-result"

    safe_globals = {
        "__builtins__": {},
        "append_audit_assessment": append_audit_assessment,
        "assess_request": assess_request,
        "run_socket_action": run_socket_action,
    }
    exec(compile(module, "<forge-audited-decision-suite>", "exec"), safe_globals)
    gate = safe_globals["assess_audit_then_execute"]

    approved = {"decision": "approved"}
    try:
        gate(
            {"assessment": approved, "audit_failure": True},
            "audit.jsonl",
        )
    except OSError:
        pass
    except Exception as exc:
        return f"audit failure raised unexpected {type(exc).__name__}"
    else:
        return "audit failure was swallowed and released a result"
    if events != ["policy", "audit"]:
        return "audit failure did not stop before the socket action"

    events.clear()
    rejected = {"decision": "rejected"}
    rejected_result = gate({"assessment": rejected}, "audit.jsonl")
    if events != ["policy", "audit"]:
        return "rejected decision was not audited before release"
    if rejected_result != (rejected, None):
        return "rejected decision returned an unexpected result"

    events.clear()
    approved_result = gate({"assessment": approved}, "audit.jsonl")
    if events != ["policy", "audit", "socket"]:
        return "approved action order is not policy -> audit -> socket"
    if approved_result != (approved, "socket-result"):
        return "approved decision returned an unexpected result"
    return None
