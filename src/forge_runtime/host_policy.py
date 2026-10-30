"""Host-only security decisions that model output must never override."""

from enum import Enum


class HostOnlyDecision(Enum):
    """Security decisions owned exclusively by deterministic host code."""

    TASK_SCHEMA = "task_schema"
    WORKSPACE_ROOT = "workspace_root"
    ALLOWED_FILES = "allowed_files"
    READ_ONLY_FILES = "read_only_files"
    ROLE_TOOL_PERMISSIONS = "role_tool_permissions"
    RESOURCE_LIMITS = "resource_limits"
    CHECK_DEFINITIONS = "check_definitions"
    HOST_SUITE_REGISTRY = "host_suite_registry"
    CHECK_RESULTS = "check_results"
    REPAIR_FEEDBACK = "repair_feedback"
    GENERATED_CODE_ELIGIBILITY = "generated_code_eligibility"
    MODEL_ENDPOINT = "model_endpoint"
    ROLE_TRANSITION = "role_transition"
    TASK_SUCCESS = "task_success"


HOST_ONLY_DECISIONS = frozenset(HostOnlyDecision)


def host_decision(decision: HostOnlyDecision, outcome: bool) -> bool:
    """Bind a boolean security outcome to a registered host-only decision."""
    if type(decision) is not HostOnlyDecision:
        raise ValueError("decision must be a registered HostOnlyDecision")
    if type(outcome) is not bool:
        raise ValueError("host decision outcome must be a boolean")
    return outcome
