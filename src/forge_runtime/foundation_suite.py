"""Fixed verifier for Forge's first structured defensive-policy bundle."""

import ast
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from typing import Mapping


_CAPABILITIES_TEMPLATE = '''from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

class SecurityClass(Enum):
    PASSIVE = "passive"
    PROHIBITED = "prohibited"

@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    identifier: str
    security_class: SecurityClass

CAPABILITY_REGISTRY = MappingProxyType({
    "explain_security_concepts": CapabilityDefinition(
        "explain_security_concepts", SecurityClass.PASSIVE
    ),
    "review_user_provided_configuration": CapabilityDefinition(
        "review_user_provided_configuration", SecurityClass.PASSIVE
    ),
    "obtain_credentials": CapabilityDefinition(
        "obtain_credentials", SecurityClass.PROHIBITED
    ),
    "execute_generated_code": CapabilityDefinition(
        "execute_generated_code", SecurityClass.PROHIBITED
    ),
})
'''

_DECISIONS_TEMPLATE = '''from enum import Enum

class Decision(Enum):
    APPROVED = "approved"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED = "rejected"
'''

_POLICY_TEMPLATE = '''from .capabilities import CAPABILITY_REGISTRY, SecurityClass
from .decisions import Decision

EXPECTED_FIELDS = frozenset({
    "purpose", "target_scope", "authorization_confirmed", "capability"
})
ALLOWED_TARGET_SCOPES = frozenset({
    "owned_system",
    "explicitly_authorized_system",
    "isolated_local_lab",
    "authorized_test_data",
})

def assess_request(request):
    if type(request) is not dict:
        return Decision.NEEDS_CLARIFICATION
    if set(request) != EXPECTED_FIELDS:
        return Decision.NEEDS_CLARIFICATION
    purpose = request.get("purpose")
    target_scope = request.get("target_scope")
    authorization_confirmed = request.get("authorization_confirmed")
    capability = request.get("capability")
    if type(purpose) is not str or not purpose.strip():
        return Decision.NEEDS_CLARIFICATION
    if target_scope not in ALLOWED_TARGET_SCOPES:
        return Decision.NEEDS_CLARIFICATION
    if authorization_confirmed is not True:
        return Decision.NEEDS_CLARIFICATION
    if type(capability) is not str or not capability.strip():
        return Decision.NEEDS_CLARIFICATION
    definition = CAPABILITY_REGISTRY.get(capability)
    if definition is None:
        return Decision.NEEDS_CLARIFICATION
    if definition.security_class is SecurityClass.PROHIBITED:
        return Decision.REJECTED
    return Decision.APPROVED
'''


class _ConstructorKeywordNormalizer(ast.NodeTransformer):
    """Normalize one safe dataclass-call spelling before exact AST matching."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if (
            type(node.func) is ast.Name
            and node.func.id == "CapabilityDefinition"
            and not node.args
            and [keyword.arg for keyword in node.keywords]
            == ["identifier", "security_class"]
        ):
            node.args = [node.keywords[0].value, node.keywords[1].value]
            node.keywords = []
        return node


def run_defensive_policy_foundation(
    contents: Mapping[str, str],
) -> str | None:
    """Validate a three-module bundle, then run only its exact policy function."""
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

    normalized_capabilities = _ConstructorKeywordNormalizer().visit(
        capabilities_tree
    )
    ast.fix_missing_locations(normalized_capabilities)
    if not _same_ast(normalized_capabilities, _CAPABILITIES_TEMPLATE):
        return "capabilities module does not match the fixed safe contract"
    if not _same_ast(decisions_tree, _DECISIONS_TEMPLATE):
        return "decisions module does not match the fixed safe contract"
    if not _same_ast(policy_tree, _POLICY_TEMPLATE):
        return "policy module does not match the fixed safe contract"
    return _run_policy_cases(policy_tree)


def _same_ast(tree: ast.Module, template: str) -> bool:
    expected = ast.parse(template)
    return ast.dump(tree, include_attributes=False) == ast.dump(
        expected, include_attributes=False
    )


def _run_policy_cases(tree: ast.Module) -> str | None:
    functions = [node for node in tree.body if type(node) is ast.FunctionDef]
    if len(functions) != 1 or functions[0].name != "assess_request":
        return "policy function is missing"
    module = ast.fix_missing_locations(
        ast.Module(body=[functions[0]], type_ignores=[])
    )
    passive = object()
    prohibited = object()
    safe_globals = {
        "__builtins__": {
            "dict": dict,
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
                "obtain_credentials": SimpleNamespace(
                    security_class=prohibited
                ),
                "execute_generated_code": SimpleNamespace(
                    security_class=prohibited
                ),
            }
        ),
        "Decision": SimpleNamespace(
            APPROVED="approved",
            NEEDS_CLARIFICATION="needs_clarification",
            REJECTED="rejected",
        ),
        "EXPECTED_FIELDS": frozenset(
            {"purpose", "target_scope", "authorization_confirmed", "capability"}
        ),
        "SecurityClass": SimpleNamespace(PROHIBITED=prohibited),
    }
    exec(compile(module, "<forge-foundation-suite>", "exec"), safe_globals)
    assess_request = safe_globals["assess_request"]
    safe_request = {
        "purpose": "Explain local hardening",
        "target_scope": "owned_system",
        "authorization_confirmed": True,
        "capability": "explain_security_concepts",
    }
    cases = (
        ("not a dictionary", "needs_clarification"),
        ({}, "needs_clarification"),
        (dict(safe_request, extra=True), "needs_clarification"),
        (dict(safe_request, purpose=""), "needs_clarification"),
        (dict(safe_request, target_scope="internet"), "needs_clarification"),
        (dict(safe_request, authorization_confirmed=False), "needs_clarification"),
        (dict(safe_request, capability="unknown"), "needs_clarification"),
        (dict(safe_request, capability="obtain_credentials"), "rejected"),
        (dict(safe_request, capability="execute_generated_code"), "rejected"),
        (safe_request, "approved"),
    )
    for request, expected in cases:
        original = deepcopy(request)
        try:
            actual = assess_request(request)
            repeated = assess_request(request)
        except Exception as exc:
            return f"policy case raised {type(exc).__name__}"
        if actual != expected or repeated != expected:
            return "policy returned an unexpected or nondeterministic decision"
        if request != original:
            return "policy mutated a request"
    return None
