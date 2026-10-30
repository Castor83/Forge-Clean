"""Fixed host implementation for the one bounded active lab capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from ipaddress import ip_address
from pathlib import Path
import socket
from threading import Event, RLock
from time import monotonic
from weakref import ReferenceType, ref

from .decision_service import assess_and_record_request
from .execution_contract import ValidatedExecutionContract, contract_is_current


class ExecutionStatusCode(Enum):
    COMPLETED = "EXECUTION_COMPLETED"
    AUTHORIZATION_REVOKED = "EXECUTION_AUTHORIZATION_REVOKED"
    DEADLINE_REACHED = "EXECUTION_DEADLINE_REACHED"
    HOST_POLICY_DENIED = "EXECUTION_HOST_POLICY_DENIED"
    RESOURCE_LIMIT_REACHED = "EXECUTION_RESOURCE_LIMIT_REACHED"
    TARGET_MISMATCH = "EXECUTION_TARGET_MISMATCH"
    INTERNAL_ERROR = "EXECUTION_INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class PortProbeResult:
    port: int
    status: str


@dataclass(frozen=True, slots=True)
class ActiveScanResult:
    status_code: str
    completed: bool
    attempts: int
    probes: tuple[PortProbeResult, ...]


_PROBE_AUTHORIZATION_SEAL = object()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _AuthorizedProbe:
    address: str
    port: int
    transport: str
    _seal: object = field(repr=False)
    _used: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self._seal is not _PROBE_AUTHORIZATION_SEAL:
            raise TypeError("probe authorization must be issued by ExecutionGuard")

    def consume(self) -> tuple[str, int, str]:
        with _PROBE_ISSUANCE_LOCK:
            entry = _ISSUED_PROBES.get(id(self))
            if (
                type(self) is not _AuthorizedProbe
                or entry is None
                or entry[0]() is not self
            ):
                if getattr(self, "_used", False):
                    raise ValueError("probe authorization has already been consumed")
                raise TypeError("authorization must be issued by ExecutionGuard")
            # Remove the one-time issuance before any socket or boundary check.
            # Resetting _used or copying this object cannot restore its issuance.
            del _ISSUED_PROBES[id(self)]
            was_used = getattr(self, "_used", True)
            object.__setattr__(self, "_used", True)
            if was_used:
                raise ValueError("probe authorization has already been consumed")
            if (
                getattr(self, "_seal", None) is not _PROBE_AUTHORIZATION_SEAL
                or _probe_content(self) != entry[1]
            ):
                raise ValueError("probe authorization no longer matches its host issuance")
            return entry[1]


_ISSUED_PROBES: dict[
    int, tuple[ReferenceType[_AuthorizedProbe], tuple[str, int, str]]
] = {}
_PROBE_ISSUANCE_LOCK = RLock()


def _probe_content(authorization: _AuthorizedProbe) -> tuple[str, int, str] | None:
    try:
        address, port, transport = (
            authorization.address,
            authorization.port,
            authorization.transport,
        )
    except AttributeError:
        return None
    if type(address) is not str or type(port) is not int or type(transport) is not str:
        return None
    return address, port, transport


_ISSUED_GUARDS: dict[
    int, tuple[ReferenceType[ExecutionGuard], ReferenceType[ValidatedExecutionContract]]
] = {}
_GUARD_ISSUANCE_LOCK = RLock()


def _require_issued_guard(guard: object) -> None:
    with _GUARD_ISSUANCE_LOCK:
        entry = _ISSUED_GUARDS.get(id(guard))
        if not (
            type(guard) is ExecutionGuard
            and entry is not None
            and entry[0]() is guard
            and entry[1]() is getattr(guard, "_contract", None)
        ):
            raise TypeError("ExecutionGuard must be issued by the host constructor")


class ExecutionGuard:
    """Stateful host guard checked immediately before every network attempt."""

    __slots__ = (
        "_contract",
        "_started",
        "_runtime_deadline",
        "_expiry_deadline",
        "_attempts",
        "_abort_event",
        "__weakref__",
    )

    def __init__(
        self,
        contract: ValidatedExecutionContract,
        *,
        abort_event: Event | None = None,
    ) -> None:
        if type(self) is not ExecutionGuard:
            raise TypeError("ExecutionGuard must be issued by the host constructor")
        if type(contract) is not ValidatedExecutionContract:
            raise TypeError("contract must be host validated")
        if abort_event is not None and type(abort_event) is not Event:
            raise TypeError("abort_event must be a threading.Event")
        now = _utc_now()
        if not contract_is_current(contract, now_utc=now):
            raise ValueError("contract is not current")
        expires_at = datetime.strptime(
            contract.expires_at_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        self._contract = contract
        self._started = _monotonic()
        self._runtime_deadline = (
            self._started + contract.limits.max_runtime_seconds
        )
        self._expiry_deadline = self._started + (expires_at - now).total_seconds()
        self._attempts = 0
        self._abort_event = abort_event
        identity = id(self)

        def discard(released: ReferenceType[ExecutionGuard]) -> None:
            with _GUARD_ISSUANCE_LOCK:
                entry = _ISSUED_GUARDS.get(identity)
                if entry is not None and entry[0] is released:
                    del _ISSUED_GUARDS[identity]

        with _GUARD_ISSUANCE_LOCK:
            _ISSUED_GUARDS[identity] = (ref(self, discard), ref(contract))

    @property
    def attempts(self) -> int:
        return self._attempts

    def authorize_probe(
        self,
        *,
        address: str,
        port: int,
        transport: str,
    ) -> ExecutionStatusCode | None:
        """Return None only when this exact attempt is authorized and reserved."""
        status, _authorization = self.reserve_probe(
            address=address,
            port=port,
            transport=transport,
        )
        return status

    def reserve_probe(
        self,
        *,
        address: str,
        port: int,
        transport: str,
    ) -> tuple[ExecutionStatusCode | None, _AuthorizedProbe | None]:
        """Reserve one attempt and issue its sealed, single-use authorization."""
        _require_issued_guard(self)
        abort_status = self.current_abort_status()
        if abort_status is not None:
            return abort_status, None
        if not _target_matches(self._contract, address, port, transport):
            return ExecutionStatusCode.TARGET_MISMATCH, None
        if self._attempts >= self._contract.limits.max_connections:
            return ExecutionStatusCode.RESOURCE_LIMIT_REACHED, None
        self._attempts += 1
        authorization = _AuthorizedProbe(
            address=address,
            port=port,
            transport=transport,
            _seal=_PROBE_AUTHORIZATION_SEAL,
        )
        identity = id(authorization)

        def discard(released: ReferenceType[_AuthorizedProbe]) -> None:
            with _PROBE_ISSUANCE_LOCK:
                entry = _ISSUED_PROBES.get(identity)
                if entry is not None and entry[0] is released:
                    del _ISSUED_PROBES[identity]

        content = _probe_content(authorization)
        if content is None:
            raise TypeError("probe authorization contains invalid host values")
        with _PROBE_ISSUANCE_LOCK:
            _ISSUED_PROBES[identity] = (ref(authorization, discard), content)
        return None, authorization

    def current_abort_status(self) -> ExecutionStatusCode | None:
        """Recheck revocation and both wall-clock and monotonic deadlines."""
        _require_issued_guard(self)
        if self._abort_event is not None and self._abort_event.is_set():
            return ExecutionStatusCode.AUTHORIZATION_REVOKED
        if not contract_is_current(self._contract, now_utc=_utc_now()):
            return ExecutionStatusCode.DEADLINE_REACHED
        current = _monotonic()
        if current >= self._expiry_deadline:
            return ExecutionStatusCode.DEADLINE_REACHED
        if current >= self._runtime_deadline:
            return ExecutionStatusCode.RESOURCE_LIMIT_REACHED
        return None

    def remaining_seconds(self) -> float:
        current = _monotonic()
        return max(0.0, min(self._runtime_deadline, self._expiry_deadline) - current)


def run_authorized_lab_scan(
    request: object,
    contract: ValidatedExecutionContract,
    *,
    audit_path: Path,
    abort_event: Event | None = None,
) -> ActiveScanResult:
    """Perform TCP connect probes through fixed code, without sending payload data."""
    now = _utc_now()
    assessment = assess_and_record_request(
        request,
        audit_path=audit_path,
        validated_contract=contract,
        now_utc=now,
    )
    if assessment["decision"] != "approved":
        return _result(ExecutionStatusCode.HOST_POLICY_DENIED, 0, ())

    try:
        guard = ExecutionGuard(contract, abort_event=abort_event)
    except (TypeError, ValueError):
        return _result(ExecutionStatusCode.HOST_POLICY_DENIED, 0, ())

    probes: list[PortProbeResult] = []
    for port in contract.target.ports:
        status, authorization = guard.reserve_probe(
            address=contract.target.address,
            port=port,
            transport=contract.target.transport,
        )
        if status is not None:
            return _result(status, guard.attempts, tuple(probes))
        if authorization is None:
            return _result(
                ExecutionStatusCode.INTERNAL_ERROR, guard.attempts, tuple(probes)
            )
        remaining = guard.remaining_seconds()
        if remaining <= 0:
            return _result(
                ExecutionStatusCode.RESOURCE_LIMIT_REACHED,
                guard.attempts,
                tuple(probes),
            )
        try:
            is_open = _tcp_connect_probe(
                authorization,
                timeout_seconds=min(2.0, remaining),
            )
        except OSError:
            return _result(
                ExecutionStatusCode.INTERNAL_ERROR, guard.attempts, tuple(probes)
            )
        probes.append(PortProbeResult(port=port, status="open" if is_open else "closed"))
        post_attempt_status = guard.current_abort_status()
        if post_attempt_status is not None:
            return _result(post_attempt_status, guard.attempts, tuple(probes))

    return _result(ExecutionStatusCode.COMPLETED, guard.attempts, tuple(probes))


def _target_matches(
    contract: ValidatedExecutionContract,
    address: object,
    port: object,
    transport: object,
) -> bool:
    if type(address) is not str or type(port) is not int or type(transport) is not str:
        return False
    try:
        canonical_address = str(ip_address(address))
    except ValueError:
        return False
    return (
        canonical_address == contract.target.address
        and port in contract.target.ports
        and transport == contract.target.transport
    )


def _tcp_connect_probe(
    authorization: _AuthorizedProbe,
    *,
    timeout_seconds: float,
) -> bool:
    if type(authorization) is not _AuthorizedProbe:
        raise TypeError("authorization must be issued by ExecutionGuard")
    address, port, transport = authorization.consume()
    parsed = ip_address(address)
    if not parsed.is_loopback or transport != "tcp":
        raise ValueError("probe authorization is outside the fixed host boundary")
    family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
    endpoint = (
        (address, port)
        if parsed.version == 4
        else (address, port, 0, 0)
    )
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout_seconds)
        return connection.connect_ex(endpoint) == 0


def _result(
    status: ExecutionStatusCode,
    attempts: int,
    probes: tuple[PortProbeResult, ...],
) -> ActiveScanResult:
    return ActiveScanResult(
        status_code=status.value,
        completed=status is ExecutionStatusCode.COMPLETED,
        attempts=attempts,
        probes=probes,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _monotonic() -> float:
    return monotonic()
