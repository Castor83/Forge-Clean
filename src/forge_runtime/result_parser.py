import json

from .contracts import SubagentResult


_RESULT_FIELDS = {"role", "passed", "summary", "findings"}
_ROLES = ("builder", "tester", "reviewer")


def parse_subagent_result(expected_role: str, response: str) -> SubagentResult:
    """Parse one strict JSON result returned by a Forge subagent."""
    if type(expected_role) is not str or expected_role not in _ROLES:
        raise ValueError("expected_role must be builder, tester, or reviewer")
    if type(response) is not str:
        raise ValueError("response must be a string")

    try:
        data = json.loads(response)
    except json.JSONDecodeError as exc:
        raise ValueError("response must be valid JSON") from exc

    if type(data) is not dict:
        raise ValueError("response must contain a JSON object")
    if set(data) != _RESULT_FIELDS:
        raise ValueError("response must contain exactly role, passed, summary, and findings")
    if type(data["role"]) is not str or data["role"] != expected_role:
        raise ValueError("response role must match expected_role")
    if type(data["passed"]) is not bool:
        raise ValueError("response passed must be a boolean")
    if type(data["summary"]) is not str or not data["summary"].strip():
        raise ValueError("response summary must be a non-empty string")
    if type(data["findings"]) is not list:
        raise ValueError("response findings must be a list")
    if any(type(item) is not str or not item.strip() for item in data["findings"]):
        raise ValueError("response findings must contain non-empty strings")

    return SubagentResult(
        role=data["role"],
        passed=data["passed"],
        summary=data["summary"],
        findings=tuple(data["findings"]),
    )
