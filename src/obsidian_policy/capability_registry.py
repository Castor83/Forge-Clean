"""Typed, immutable capability metadata for the Obsidian policy gate."""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class SecurityClass(Enum):
    """Security meaning enforced by the policy gate."""

    PASSIVE = "passive"
    ACTIVE = "active"
    PROHIBITED = "prohibited"


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """Immutable metadata; policy decisions deliberately do not live here."""

    identifier: str
    security_class: SecurityClass
    contract_type: str | None = None

    def __post_init__(self) -> None:
        if type(self.identifier) is not str or not self.identifier.strip():
            raise ValueError("identifier must be a non-empty string")
        if not isinstance(self.security_class, SecurityClass):
            raise ValueError("security_class must be a known SecurityClass")
        if self.security_class is SecurityClass.ACTIVE:
            if type(self.contract_type) is not str or not self.contract_type.strip():
                raise ValueError("active capabilities must name a contract_type")
        elif self.contract_type is not None:
            raise ValueError("only active capabilities may name a contract_type")


def _build_registry(
    definitions: tuple[CapabilityDefinition, ...],
) -> Mapping[str, CapabilityDefinition]:
    registry: dict[str, CapabilityDefinition] = {}
    for definition in definitions:
        if definition.identifier in registry:
            raise ValueError(f"duplicate capability: {definition.identifier}")
        registry[definition.identifier] = definition
    return MappingProxyType(registry)


CAPABILITY_REGISTRY = _build_registry(
    (
        CapabilityDefinition("explain_security_concepts", SecurityClass.PASSIVE),
        CapabilityDefinition(
            "review_user_provided_configuration", SecurityClass.PASSIVE
        ),
        CapabilityDefinition("analyze_user_provided_logs", SecurityClass.PASSIVE),
        CapabilityDefinition("assess_local_lab_fixture", SecurityClass.PASSIVE),
        CapabilityDefinition("create_defensive_checklist", SecurityClass.PASSIVE),
        CapabilityDefinition(
            "run_authorized_lab_scan",
            SecurityClass.ACTIVE,
            contract_type="authorized_lab_scan_v1",
        ),
        CapabilityDefinition("establish_persistence", SecurityClass.PROHIBITED),
        CapabilityDefinition("evade_detection", SecurityClass.PROHIBITED),
        CapabilityDefinition("obtain_credentials", SecurityClass.PROHIBITED),
        CapabilityDefinition("exfiltrate_data", SecurityClass.PROHIBITED),
        CapabilityDefinition("disrupt_system", SecurityClass.PROHIBITED),
        CapabilityDefinition(
            "target_unauthorized_system", SecurityClass.PROHIBITED
        ),
        CapabilityDefinition("execute_generated_code", SecurityClass.PROHIBITED),
    )
)
