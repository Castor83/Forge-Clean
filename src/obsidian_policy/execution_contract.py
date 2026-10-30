"""Host-side validation for bounded Obsidian active-operation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from ipaddress import ip_address
import json
from threading import RLock
from types import MappingProxyType
from typing import Mapping
from uuid import UUID
from weakref import ReferenceType, ref


CONTRACT_TYPE = "authorized_lab_scan_v1"
ALLOWED_ACTIONS = frozenset({"tcp_connect_probe"})
REQUIRED_APPROVAL_POINTS = frozenset({"before_execution"})
REQUIRED_ABORT_CONDITIONS = frozenset(
    {
        "authorization_revoked",
        "deadline_reached",
        "host_policy_denied",
        "resource_limit_reached",
        "target_mismatch",
    }
)
MAX_PORTS = 16
MAX_VALIDITY_SECONDS = 15 * 60
MAX_CONNECTIONS = 64
MAX_CONCURRENCY = 4
MAX_RUNTIME_SECONDS = 60

_CONTRACT_FIELDS = frozenset(
    {
        "contract_id",
        "contract_type",
        "capability",
        "target_scope",
        "target",
        "validity",
        "limits",
        "allowed_actions",
        "approval_points",
        "abort_conditions",
    }
)
_TARGET_FIELDS = frozenset({"kind", "address", "ports", "transport"})
_VALIDITY_FIELDS = frozenset({"not_before_utc", "expires_at_utc"})
_LIMIT_FIELDS = frozenset(
    {"max_connections", "max_concurrency", "max_runtime_seconds", "max_bytes_sent"}
)
_VALIDATION_SEAL = object()
_REQUEST_FIELDS = frozenset(
    {"purpose", "target_scope", "authorization_confirmed", "capabilities"}
)


class ContractValidationError(ValueError):
    """A raw contract failed closed validation."""


@dataclass(frozen=True, slots=True)
class TargetBinding:
    kind: str
    address: str
    ports: tuple[int, ...]
    transport: str


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_connections: int
    max_concurrency: int
    max_runtime_seconds: int
    max_bytes_sent: int


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ValidatedExecutionContract:
    """Immutable contract produced only after host-side validation."""

    contract_id: str
    contract_type: str
    capability: str
    target_scope: str
    request_hash: str
    target: TargetBinding
    not_before_utc: str
    expires_at_utc: str
    limits: ResourceLimits
    allowed_actions: frozenset[str]
    approval_points: frozenset[str]
    abort_conditions: frozenset[str]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VALIDATION_SEAL:
            raise ContractValidationError(
                "validated contracts must be created by the host validator"
            )


_ISSUED_CONTRACTS: dict[
    int, tuple[ReferenceType[ValidatedExecutionContract], tuple[object, ...]]
] = {}
_CONTRACT_ISSUANCE_LOCK = RLock()


def validate_execution_contract(
    raw_contract: object,
    *,
    bound_request: object,
    now_utc: datetime,
) -> ValidatedExecutionContract:
    """Validate untrusted contract data and return an immutable host artifact."""
    contract = _exact_dict(raw_contract, _CONTRACT_FIELDS, "contract")
    now = _utc_datetime(now_utc, "now_utc")

    contract_id = _canonical_uuid(contract["contract_id"])
    if contract["contract_type"] != CONTRACT_TYPE:
        raise ContractValidationError("contract_type is not supported")
    if contract["capability"] != "run_authorized_lab_scan":
        raise ContractValidationError("capability is not supported by contract_type")
    if contract["target_scope"] != "isolated_local_lab":
        raise ContractValidationError("target_scope must be isolated_local_lab")
    request_hash = _validate_bound_request(
        bound_request,
        capability="run_authorized_lab_scan",
        target_scope="isolated_local_lab",
    )

    target = _validate_target(contract["target"])
    not_before, expires_at = _validate_validity(contract["validity"], now)
    limits = _validate_limits(contract["limits"])
    allowed_actions = _exact_string_set(
        contract["allowed_actions"], ALLOWED_ACTIONS, "allowed_actions"
    )
    approval_points = _exact_string_set(
        contract["approval_points"], REQUIRED_APPROVAL_POINTS, "approval_points"
    )
    abort_conditions = _exact_string_set(
        contract["abort_conditions"], REQUIRED_ABORT_CONDITIONS, "abort_conditions"
    )

    validated = ValidatedExecutionContract(
        contract_id=contract_id,
        contract_type=CONTRACT_TYPE,
        capability="run_authorized_lab_scan",
        target_scope="isolated_local_lab",
        request_hash=request_hash,
        target=target,
        not_before_utc=_format_utc(not_before),
        expires_at_utc=_format_utc(expires_at),
        limits=limits,
        allowed_actions=allowed_actions,
        approval_points=approval_points,
        abort_conditions=abort_conditions,
        _seal=_VALIDATION_SEAL,
    )
    # Only this path records issuance, after all raw fields have been validated.
    # Store values as well as identity so reflected changes invalidate issuance.
    identity = id(validated)

    def discard(released: ReferenceType[ValidatedExecutionContract]) -> None:
        with _CONTRACT_ISSUANCE_LOCK:
            entry = _ISSUED_CONTRACTS.get(identity)
            if entry is not None and entry[0] is released:
                del _ISSUED_CONTRACTS[identity]

    content = _contract_content(validated)
    if content is None:
        raise ContractValidationError("validated contract contains invalid host values")
    with _CONTRACT_ISSUANCE_LOCK:
        _ISSUED_CONTRACTS[identity] = (
            ref(validated, discard),
            content,
        )
    return validated


def _contract_was_issued(contract: object) -> bool:
    with _CONTRACT_ISSUANCE_LOCK:
        entry = _ISSUED_CONTRACTS.get(id(contract))
        return (
            type(contract) is ValidatedExecutionContract
            and entry is not None
            and entry[0]() is contract
            and getattr(contract, "_seal", None) is _VALIDATION_SEAL
            and _contract_content(contract) == entry[1]
        )


def _contract_content(
    contract: ValidatedExecutionContract,
) -> tuple[object, ...] | None:
    """Snapshot only exact immutable values; never retain the issued object."""
    try:
        target = contract.target
        limits = contract.limits
        if type(target) is not TargetBinding or type(limits) is not ResourceLimits:
            return None
        content = (
            contract.contract_id,
            contract.contract_type,
            contract.capability,
            contract.target_scope,
            contract.request_hash,
            (target.kind, target.address, target.ports, target.transport),
            contract.not_before_utc,
            contract.expires_at_utc,
            (
                limits.max_connections,
                limits.max_concurrency,
                limits.max_runtime_seconds,
                limits.max_bytes_sent,
            ),
            contract.allowed_actions,
            contract.approval_points,
            contract.abort_conditions,
        )
    except AttributeError:
        return None
    return content if _is_immutable_value(content) else None


def _is_immutable_value(value: object) -> bool:
    if type(value) in (str, int):
        return True
    if type(value) in (tuple, frozenset):
        return all(_is_immutable_value(item) for item in value)
    return False


def request_matches_contract(
    request: object, contract: ValidatedExecutionContract
) -> bool:
    """Check that a contract is used only for the exact request it approved."""
    if not _contract_was_issued(contract):
        return False
    try:
        request_hash = _validate_bound_request(
            request,
            capability=contract.capability,
            target_scope=contract.target_scope,
        )
    except ContractValidationError:
        return False
    return request_hash == contract.request_hash


def contract_is_current(
    contract: ValidatedExecutionContract,
    *,
    now_utc: datetime,
) -> bool:
    """Recheck the validity window immediately before a host operation."""
    if not _contract_was_issued(contract):
        return False
    now = _utc_datetime(now_utc, "now_utc")
    return _parse_utc(contract.not_before_utc, "not_before_utc") <= now < _parse_utc(
        contract.expires_at_utc, "expires_at_utc"
    )


def _validate_target(value: object) -> TargetBinding:
    target = _exact_dict(value, _TARGET_FIELDS, "target")
    if target["kind"] != "ip_literal":
        raise ContractValidationError("target.kind must be ip_literal")
    if type(target["address"]) is not str:
        raise ContractValidationError("target.address must be an IP literal")
    try:
        parsed_address = ip_address(target["address"])
    except ValueError as exc:
        raise ContractValidationError("target.address must be an IP literal") from exc
    if not parsed_address.is_loopback:
        raise ContractValidationError("target.address must be loopback")
    if target["transport"] != "tcp":
        raise ContractValidationError("target.transport must be tcp")

    ports_value = target["ports"]
    if type(ports_value) is not list or not ports_value:
        raise ContractValidationError("target.ports must be a non-empty list")
    if len(ports_value) > MAX_PORTS:
        raise ContractValidationError("target.ports exceeds the host limit")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports_value):
        raise ContractValidationError("target.ports contains an invalid port")
    if len(set(ports_value)) != len(ports_value):
        raise ContractValidationError("target.ports contains duplicates")
    return TargetBinding(
        kind="ip_literal",
        address=str(parsed_address),
        ports=tuple(sorted(ports_value)),
        transport="tcp",
    )


def _validate_bound_request(
    request: object, *, capability: str, target_scope: str
) -> str:
    parsed = _exact_dict(request, _REQUEST_FIELDS, "bound_request")
    if type(parsed["purpose"]) is not str or not parsed["purpose"].strip():
        raise ContractValidationError("bound_request.purpose is invalid")
    if parsed["target_scope"] != target_scope:
        raise ContractValidationError("bound_request.target_scope does not match")
    if parsed["authorization_confirmed"] is not True:
        raise ContractValidationError("bound_request authorization is not confirmed")
    if parsed["capabilities"] != [capability]:
        raise ContractValidationError("bound_request capability does not match")
    try:
        canonical = json.dumps(
            parsed,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("bound_request is not canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def _validate_validity(
    value: object, now: datetime
) -> tuple[datetime, datetime]:
    validity = _exact_dict(value, _VALIDITY_FIELDS, "validity")
    not_before = _parse_utc(validity["not_before_utc"], "not_before_utc")
    expires_at = _parse_utc(validity["expires_at_utc"], "expires_at_utc")
    duration = (expires_at - not_before).total_seconds()
    if duration <= 0 or duration > MAX_VALIDITY_SECONDS:
        raise ContractValidationError("validity window is invalid or too long")
    if not not_before <= now < expires_at:
        raise ContractValidationError("contract is not currently valid")
    return not_before, expires_at


def _validate_limits(value: object) -> ResourceLimits:
    limits = _exact_dict(value, _LIMIT_FIELDS, "limits")
    maxima = MappingProxyType(
        {
            "max_connections": MAX_CONNECTIONS,
            "max_concurrency": MAX_CONCURRENCY,
            "max_runtime_seconds": MAX_RUNTIME_SECONDS,
        }
    )
    for name, maximum in maxima.items():
        item = limits[name]
        if type(item) is not int or not 1 <= item <= maximum:
            raise ContractValidationError(f"limits.{name} is outside the host limit")
    if type(limits["max_bytes_sent"]) is not int or limits["max_bytes_sent"] != 0:
        raise ContractValidationError("limits.max_bytes_sent must be zero")
    return ResourceLimits(
        max_connections=limits["max_connections"],
        max_concurrency=limits["max_concurrency"],
        max_runtime_seconds=limits["max_runtime_seconds"],
        max_bytes_sent=0,
    )


def _exact_dict(
    value: object, expected_fields: frozenset[str], label: str
) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ContractValidationError(f"{label} must be a dictionary")
    keys = list(value)
    if any(type(key) is not str for key in keys):
        raise ContractValidationError(f"{label} field names must be strings")
    actual = set(keys)
    if actual != expected_fields:
        raise ContractValidationError(f"{label} fields do not match the schema")
    return value


def _exact_string_set(
    value: object, expected: frozenset[str], label: str
) -> frozenset[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ContractValidationError(f"{label} must be a list of strings")
    if len(value) != len(set(value)):
        raise ContractValidationError(f"{label} contains duplicates")
    if frozenset(value) != expected:
        raise ContractValidationError(f"{label} does not match host requirements")
    return expected


def _canonical_uuid(value: object) -> str:
    if type(value) is not str:
        raise ContractValidationError("contract_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ContractValidationError("contract_id must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ContractValidationError("contract_id must be a canonical UUID")
    return value


def _utc_datetime(value: object, label: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ContractValidationError(f"{label} must be timezone-aware UTC")
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond != 0:
        raise ContractValidationError(f"{label} must be whole-second UTC")
    return normalized


def _parse_utc(value: object, label: str) -> datetime:
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        raise ContractValidationError(f"{label} must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ContractValidationError(f"{label} must use YYYY-MM-DDTHH:MM:SSZ") from exc
    return parsed


def _format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
