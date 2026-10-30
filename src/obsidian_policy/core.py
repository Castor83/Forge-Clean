"""Conservative, side-effect-free request gate for Obsidian."""

from enum import Enum
from datetime import datetime, timezone

from .capability_registry import CAPABILITY_REGISTRY, SecurityClass
from .execution_contract import (
    ValidatedExecutionContract,
    contract_is_current,
    request_matches_contract,
)


class DecisionCode(Enum):
    APPROVED = "REQUEST_APPROVED"
    NEEDS_CLARIFICATION = "REQUEST_NEEDS_CLARIFICATION"
    REJECTED = "REQUEST_REJECTED"


class ReasonCode(Enum):
    REQUEST_TYPE_INVALID = "REQUEST_TYPE_INVALID"
    FIELD_NAME_INVALID = "FIELD_NAME_INVALID"
    FIELD_MISSING = "FIELD_MISSING"
    FIELD_UNKNOWN = "FIELD_UNKNOWN"
    PURPOSE_INVALID = "PURPOSE_INVALID"
    TARGET_SCOPE_INVALID = "TARGET_SCOPE_INVALID"
    AUTHORIZATION_REQUIRED = "AUTHORIZATION_REQUIRED"
    CAPABILITIES_INVALID = "CAPABILITIES_INVALID"
    CAPABILITY_ITEM_INVALID = "CAPABILITY_ITEM_INVALID"
    CAPABILITY_DUPLICATE = "CAPABILITY_DUPLICATE"
    CAPABILITY_PROHIBITED = "CAPABILITY_PROHIBITED"
    CAPABILITY_ACTIVE_CONTRACT_REQUIRED = "CAPABILITY_ACTIVE_CONTRACT_REQUIRED"
    CAPABILITY_ACTIVE_CONTRACT_VALIDATED = "CAPABILITY_ACTIVE_CONTRACT_VALIDATED"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    REQUEST_WITHIN_DEFENSIVE_SCOPE = "REQUEST_WITHIN_DEFENSIVE_SCOPE"

EXPECTED_REQUEST_FIELDS = frozenset(
    {"purpose", "target_scope", "authorization_confirmed", "capabilities"}
)

ALLOWED_TARGET_SCOPES = frozenset(
    {
        "owned_system",
        "explicitly_authorized_system",
        "isolated_local_lab",
        "authorized_test_data",
    }
)


def assess_request(
    request: object,
    *,
    validated_contract: ValidatedExecutionContract | None = None,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Classify a structured request without executing or modifying anything."""
    if type(request) is not dict:
        return _result(
            "needs_clarification",
            ["request must be a dictionary"],
            [ReasonCode.REQUEST_TYPE_INVALID],
        )

    reasons: list[str] = []
    reason_codes: list[ReasonCode] = []
    keys = list(request)
    non_string_keys = [key for key in keys if type(key) is not str]
    if non_string_keys:
        reasons.append("all request field names must be strings")
        reason_codes.append(ReasonCode.FIELD_NAME_INVALID)
    string_keys = {key for key in keys if type(key) is str}
    missing_fields = sorted(EXPECTED_REQUEST_FIELDS - string_keys)
    unknown_fields = sorted(string_keys - EXPECTED_REQUEST_FIELDS)
    reasons.extend(f"missing field: {field}" for field in missing_fields)
    reason_codes.extend(ReasonCode.FIELD_MISSING for _field in missing_fields)
    reasons.extend(f"unknown field: {field}" for field in unknown_fields)
    reason_codes.extend(ReasonCode.FIELD_UNKNOWN for _field in unknown_fields)

    purpose = request.get("purpose")
    target_scope = request.get("target_scope")
    authorization_confirmed = request.get("authorization_confirmed")
    capabilities = request.get("capabilities")

    if type(purpose) is not str or not purpose.strip():
        reasons.append("purpose must be a non-empty string")
        reason_codes.append(ReasonCode.PURPOSE_INVALID)

    if type(target_scope) is not str or target_scope not in ALLOWED_TARGET_SCOPES:
        reasons.append("target_scope must name an allowed authorized environment")
        reason_codes.append(ReasonCode.TARGET_SCOPE_INVALID)

    if authorization_confirmed is not True:
        reasons.append("authorization_confirmed must be true")
        reason_codes.append(ReasonCode.AUTHORIZATION_REQUIRED)

    if type(capabilities) is not list or not capabilities:
        reasons.append("capabilities must be a non-empty list")
        reason_codes.append(ReasonCode.CAPABILITIES_INVALID)
        return _result("needs_clarification", reasons, reason_codes)

    normalized_capabilities: list[str] = []
    for index, capability in enumerate(capabilities):
        if type(capability) is not str or not capability.strip():
            reasons.append(f"capability at index {index} must be a non-empty string")
            reason_codes.append(ReasonCode.CAPABILITY_ITEM_INVALID)
        else:
            normalized_capabilities.append(capability.strip())

    duplicates = sorted(
        capability
        for capability in set(normalized_capabilities)
        if normalized_capabilities.count(capability) > 1
    )
    reasons.extend(f"duplicate capability: {capability}" for capability in duplicates)
    reason_codes.extend(ReasonCode.CAPABILITY_DUPLICATE for _item in duplicates)

    definitions = {
        capability: CAPABILITY_REGISTRY.get(capability)
        for capability in set(normalized_capabilities)
    }
    prohibited = sorted(
        capability
        for capability, definition in definitions.items()
        if definition is not None
        and definition.security_class is SecurityClass.PROHIBITED
    )
    if prohibited:
        return _result(
            "rejected",
            [f"prohibited capability: {capability}" for capability in prohibited],
            [ReasonCode.CAPABILITY_PROHIBITED for _item in prohibited],
        )

    active = sorted(
        capability
        for capability, definition in definitions.items()
        if definition is not None and definition.security_class is SecurityClass.ACTIVE
    )
    if active:
        contract_matches = (
            type(validated_contract) is ValidatedExecutionContract
            and len(active) == 1
            and validated_contract.capability == active[0]
            and validated_contract.contract_type == definitions[active[0]].contract_type
            and validated_contract.target_scope == target_scope
            and request_matches_contract(request, validated_contract)
            and contract_is_current(
                validated_contract,
                now_utc=now_utc or datetime.now(timezone.utc).replace(microsecond=0),
            )
        )
        if contract_matches:
            if reasons:
                return _result("needs_clarification", reasons, reason_codes)
            return _result(
                "approved",
                [
                    f"active capability has a validated execution contract: {active[0]}"
                ],
                [ReasonCode.CAPABILITY_ACTIVE_CONTRACT_VALIDATED],
            )
        return _result(
            "rejected",
            [
                f"active capability requires a validated execution contract: {capability}"
                for capability in active
            ],
            [ReasonCode.CAPABILITY_ACTIVE_CONTRACT_REQUIRED for _item in active],
        )

    unknown = sorted(
        capability
        for capability, definition in definitions.items()
        if definition is None
    )
    reasons.extend(f"unknown capability: {capability}" for capability in unknown)
    reason_codes.extend(ReasonCode.CAPABILITY_UNKNOWN for _item in unknown)

    if reasons:
        return _result("needs_clarification", reasons, reason_codes)

    return _result(
        "approved",
        ["request is within the approved defensive scope"],
        [ReasonCode.REQUEST_WITHIN_DEFENSIVE_SCOPE],
    )


def _result(
    decision: str,
    reasons: list[str],
    reason_codes: list[ReasonCode],
) -> dict[str, object]:
    decision_codes = {
        "approved": DecisionCode.APPROVED,
        "needs_clarification": DecisionCode.NEEDS_CLARIFICATION,
        "rejected": DecisionCode.REJECTED,
    }
    if len(reasons) != len(reason_codes):
        raise ValueError("every reason must have exactly one reason code")
    return {
        "decision": decision,
        "decision_code": decision_codes[decision].value,
        "reason_codes": [code.value for code in reason_codes],
        "reasons": reasons,
    }
