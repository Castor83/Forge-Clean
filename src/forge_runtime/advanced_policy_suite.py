"""Fixed verifier for a coded, multi-capability defensive policy bundle."""

import ast
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from typing import Mapping


_CAPABILITIES_TEMPLATE = '''from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

class SecurityClass(Enum):
    PASSIVE = "passive"
    ACTIVE = "active"
    PROHIBITED = "prohibited"

@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    identifier: str
    security_class: SecurityClass
    contract_type: str | None = None

CAPABILITY_REGISTRY = MappingProxyType({
    "explain_security_concepts": CapabilityDefinition(
        "explain_security_concepts", SecurityClass.PASSIVE
    ),
    "review_user_provided_configuration": CapabilityDefinition(
        "review_user_provided_configuration", SecurityClass.PASSIVE
    ),
    "run_authorized_lab_scan": CapabilityDefinition(
        "run_authorized_lab_scan",
        SecurityClass.ACTIVE,
        "authorized_lab_scan_v1",
    ),
    "obtain_credentials": CapabilityDefinition(
        "obtain_credentials", SecurityClass.PROHIBITED
    ),
    "execute_generated_code": CapabilityDefinition(
        "execute_generated_code", SecurityClass.PROHIBITED
    ),
    "target_unauthorized_system": CapabilityDefinition(
        "target_unauthorized_system", SecurityClass.PROHIBITED
    ),
})
'''

_DECISIONS_TEMPLATE = '''from enum import Enum

class DecisionCode(Enum):
    APPROVED = "REQUEST_APPROVED"
    NEEDS_CLARIFICATION = "REQUEST_NEEDS_CLARIFICATION"
    REJECTED = "REQUEST_REJECTED"

class ReasonCode(Enum):
    REQUEST_TYPE_INVALID = "REQUEST_TYPE_INVALID"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    PURPOSE_INVALID = "PURPOSE_INVALID"
    TARGET_SCOPE_INVALID = "TARGET_SCOPE_INVALID"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    CAPABILITIES_INVALID = "CAPABILITIES_INVALID"
    CAPABILITY_DUPLICATE = "CAPABILITY_DUPLICATE"
    CAPABILITY_PROHIBITED = "CAPABILITY_PROHIBITED"
    CAPABILITY_ACTIVE_CONTRACT_REQUIRED = "CAPABILITY_ACTIVE_CONTRACT_REQUIRED"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    REQUEST_WITHIN_DEFENSIVE_SCOPE = "REQUEST_WITHIN_DEFENSIVE_SCOPE"
'''

_POLICY_TEMPLATE = '''from .capabilities import CAPABILITY_REGISTRY, SecurityClass
from .decisions import DecisionCode, ReasonCode

EXPECTED_FIELDS = frozenset({
    "purpose", "target_scope", "authorization_confirmed", "capabilities"
})
ALLOWED_TARGET_SCOPES = frozenset({
    "owned_system",
    "explicitly_authorized_system",
    "isolated_local_lab",
    "authorized_test_data",
})

def _result(decision, decision_code, reason_code):
    return {
        "decision": decision,
        "decision_code": decision_code.value,
        "reason_codes": [reason_code.value],
    }

def assess_request(request):
    if type(request) is not dict:
        return _result(
            "needs_clarification",
            DecisionCode.NEEDS_CLARIFICATION,
            ReasonCode.REQUEST_TYPE_INVALID,
        )
    raw_capabilities = request.get("capabilities")
    if type(raw_capabilities) is list:
        for capability in raw_capabilities:
            if type(capability) is str:
                definition = CAPABILITY_REGISTRY.get(capability.strip())
                if (
                    definition is not None
                    and definition.security_class is SecurityClass.PROHIBITED
                ):
                    return _result(
                        "rejected",
                        DecisionCode.REJECTED,
                        ReasonCode.CAPABILITY_PROHIBITED,
                    )
    if set(request) != EXPECTED_FIELDS:
        return _result(
            "needs_clarification",
            DecisionCode.NEEDS_CLARIFICATION,
            ReasonCode.SCHEMA_INVALID,
        )
    purpose = request.get("purpose")
    target_scope = request.get("target_scope")
    authorization_confirmed = request.get("authorization_confirmed")
    if type(purpose) is not str or not purpose.strip():
        return _result(
            "needs_clarification",
            DecisionCode.NEEDS_CLARIFICATION,
            ReasonCode.PURPOSE_INVALID,
        )
    if target_scope not in ALLOWED_TARGET_SCOPES:
        return _result(
            "needs_clarification",
            DecisionCode.NEEDS_CLARIFICATION,
            ReasonCode.TARGET_SCOPE_INVALID,
        )
    if authorization_confirmed is not True:
        return _result(
            "needs_clarification",
            DecisionCode.NEEDS_CLARIFICATION,
            ReasonCode.AUTHORIZATION_REQUIRED,
        )
    if type(raw_capabilities) is not list or not raw_capabilities:
        return _result(
            "needs_clarification",
            DecisionCode.NEEDS_CLARIFICATION,
            ReasonCode.CAPABILITIES_INVALID,
        )
    normalized_capabilities = []
    for capability in raw_capabilities:
        if type(capability) is not str or not capability.strip():
            return _result(
                "needs_clarification",
                DecisionCode.NEEDS_CLARIFICATION,
                ReasonCode.CAPABILITIES_INVALID,
            )
        normalized = capability.strip()
        if normalized in normalized_capabilities:
            return _result(
                "needs_clarification",
                DecisionCode.NEEDS_CLARIFICATION,
                ReasonCode.CAPABILITY_DUPLICATE,
            )
        normalized_capabilities.append(normalized)
    for capability in normalized_capabilities:
        definition = CAPABILITY_REGISTRY.get(capability)
        if definition is None:
            return _result(
                "needs_clarification",
                DecisionCode.NEEDS_CLARIFICATION,
                ReasonCode.CAPABILITY_UNKNOWN,
            )
        if definition.security_class is SecurityClass.ACTIVE:
            return _result(
                "rejected",
                DecisionCode.REJECTED,
                ReasonCode.CAPABILITY_ACTIVE_CONTRACT_REQUIRED,
            )
    return _result(
        "approved",
        DecisionCode.APPROVED,
        ReasonCode.REQUEST_WITHIN_DEFENSIVE_SCOPE,
    )
'''


class _CapabilityCallNormalizer(ast.NodeTransformer):
    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if type(node.func) is not ast.Name or node.func.id != "CapabilityDefinition":
            return node
        keyword_names = [keyword.arg for keyword in node.keywords]
        if not node.args and keyword_names in (
            ["identifier", "security_class"],
            ["identifier", "security_class", "contract_type"],
        ):
            node.args = [keyword.value for keyword in node.keywords]
            node.keywords = []
        return node


def run_advanced_defensive_policy(contents: Mapping[str, str]) -> str | None:
    """Validate exact bundle ASTs and execute only two fixed pure functions."""
    try:
        capabilities_tree = ast.parse(
            contents["agent/capabilities.py"], filename="agent/capabilities.py"
        )
        decisions_tree = ast.parse(
            contents["agent/decisions.py"], filename="agent/decisions.py"
        )
        policy_tree = ast.parse(
            contents["agent/policy.py"], filename="agent/policy.py"
        )
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"

    normalized_capabilities = _CapabilityCallNormalizer().visit(
        capabilities_tree
    )
    ast.fix_missing_locations(normalized_capabilities)
    if not _same_ast(normalized_capabilities, _CAPABILITIES_TEMPLATE):
        return "advanced capabilities module does not match the fixed contract"
    if not _same_ast(decisions_tree, _DECISIONS_TEMPLATE):
        return "advanced decisions module does not match the fixed contract"
    if not _same_ast(policy_tree, _POLICY_TEMPLATE):
        return "advanced policy module does not match the fixed contract"
    return _run_policy_cases(policy_tree)


def _same_ast(tree: ast.Module, template: str) -> bool:
    return ast.dump(tree, include_attributes=False) == ast.dump(
        ast.parse(template), include_attributes=False
    )


def _coded_value(value: str) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _run_policy_cases(tree: ast.Module) -> str | None:
    functions = [node for node in tree.body if type(node) is ast.FunctionDef]
    if [function.name for function in functions] != ["_result", "assess_request"]:
        return "advanced policy functions are missing"
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    passive = object()
    active = object()
    prohibited = object()
    safe_globals = {
        "__builtins__": {
            "dict": dict,
            "list": list,
            "set": set,
            "str": str,
            "type": type,
        },
        "ALLOWED_TARGET_SCOPES": frozenset(
            {
                "owned_system",
                "explicitly_authorized_system",
                "isolated_local_lab",
                "authorized_test_data",
            }
        ),
        "CAPABILITY_REGISTRY": MappingProxyType(
            {
                "explain_security_concepts": SimpleNamespace(
                    security_class=passive
                ),
                "review_user_provided_configuration": SimpleNamespace(
                    security_class=passive
                ),
                "run_authorized_lab_scan": SimpleNamespace(
                    security_class=active
                ),
                "new_active_test_capability": SimpleNamespace(
                    security_class=active
                ),
                "obtain_credentials": SimpleNamespace(
                    security_class=prohibited
                ),
                "execute_generated_code": SimpleNamespace(
                    security_class=prohibited
                ),
                "target_unauthorized_system": SimpleNamespace(
                    security_class=prohibited
                ),
            }
        ),
        "DecisionCode": SimpleNamespace(
            APPROVED=_coded_value("REQUEST_APPROVED"),
            NEEDS_CLARIFICATION=_coded_value("REQUEST_NEEDS_CLARIFICATION"),
            REJECTED=_coded_value("REQUEST_REJECTED"),
        ),
        "EXPECTED_FIELDS": frozenset(
            {"purpose", "target_scope", "authorization_confirmed", "capabilities"}
        ),
        "ReasonCode": SimpleNamespace(
            **{
                name: _coded_value(name)
                for name in (
                    "REQUEST_TYPE_INVALID",
                    "SCHEMA_INVALID",
                    "PURPOSE_INVALID",
                    "TARGET_SCOPE_INVALID",
                    "AUTHORIZATION_REQUIRED",
                    "CAPABILITIES_INVALID",
                    "CAPABILITY_DUPLICATE",
                    "CAPABILITY_PROHIBITED",
                    "CAPABILITY_ACTIVE_CONTRACT_REQUIRED",
                    "CAPABILITY_UNKNOWN",
                    "REQUEST_WITHIN_DEFENSIVE_SCOPE",
                )
            }
        ),
        "SecurityClass": SimpleNamespace(
            ACTIVE=active,
            PROHIBITED=prohibited,
        ),
    }
    exec(compile(module, "<forge-advanced-policy-suite>", "exec"), safe_globals)
    assess_request = safe_globals["assess_request"]
    safe_request = {
        "purpose": "Review my local configuration",
        "target_scope": "owned_system",
        "authorization_confirmed": True,
        "capabilities": ["review_user_provided_configuration"],
    }
    cases = (
        ("not a dictionary", "needs_clarification", "REQUEST_TYPE_INVALID"),
        ({}, "needs_clarification", "SCHEMA_INVALID"),
        (
            dict(safe_request, unexpected=True),
            "needs_clarification",
            "SCHEMA_INVALID",
        ),
        (
            dict(safe_request, purpose=""),
            "needs_clarification",
            "PURPOSE_INVALID",
        ),
        (
            dict(safe_request, target_scope="internet"),
            "needs_clarification",
            "TARGET_SCOPE_INVALID",
        ),
        (
            dict(safe_request, authorization_confirmed=False),
            "needs_clarification",
            "AUTHORIZATION_REQUIRED",
        ),
        (
            dict(safe_request, capabilities=[]),
            "needs_clarification",
            "CAPABILITIES_INVALID",
        ),
        (
            dict(
                safe_request,
                capabilities=[
                    "review_user_provided_configuration",
                    " review_user_provided_configuration ",
                ],
            ),
            "needs_clarification",
            "CAPABILITY_DUPLICATE",
        ),
        (
            dict(safe_request, capabilities=["unknown"]),
            "needs_clarification",
            "CAPABILITY_UNKNOWN",
        ),
        (
            dict(safe_request, capabilities=["run_authorized_lab_scan"]),
            "rejected",
            "CAPABILITY_ACTIVE_CONTRACT_REQUIRED",
        ),
        (
            dict(safe_request, capabilities=["new_active_test_capability"]),
            "rejected",
            "CAPABILITY_ACTIVE_CONTRACT_REQUIRED",
        ),
        (
            {"capabilities": ["obtain_credentials"]},
            "rejected",
            "CAPABILITY_PROHIBITED",
        ),
        (
            dict(safe_request, capabilities=["execute_generated_code"]),
            "rejected",
            "CAPABILITY_PROHIBITED",
        ),
        (
            dict(safe_request, capabilities=["target_unauthorized_system"]),
            "rejected",
            "CAPABILITY_PROHIBITED",
        ),
        (safe_request, "approved", "REQUEST_WITHIN_DEFENSIVE_SCOPE"),
        (
            dict(
                safe_request,
                capabilities=[
                    "explain_security_concepts",
                    "review_user_provided_configuration",
                ],
            ),
            "approved",
            "REQUEST_WITHIN_DEFENSIVE_SCOPE",
        ),
    )
    decision_codes = {
        "approved": "REQUEST_APPROVED",
        "needs_clarification": "REQUEST_NEEDS_CLARIFICATION",
        "rejected": "REQUEST_REJECTED",
    }
    for request, decision, reason_code in cases:
        original = deepcopy(request)
        try:
            result = assess_request(request)
            repeated = assess_request(request)
        except Exception as exc:
            return f"advanced policy case raised {type(exc).__name__}"
        expected = {
            "decision": decision,
            "decision_code": decision_codes[decision],
            "reason_codes": [reason_code],
        }
        if result != expected or repeated != expected:
            return "advanced policy returned an unexpected result"
        if request != original:
            return "advanced policy mutated a request"
    return None
