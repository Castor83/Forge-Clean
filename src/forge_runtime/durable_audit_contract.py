"""Frozen reviewed complete audit storage contract."""

EXPECTED_SOURCE = r'''"""Minimal tamper-evident local audit chain for Obsidian policy results."""

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import UUID

from .audit_acl import (
    AuditAclError,
    ensure_audit_storage_is_secure,
    flush_audit_directory,
    harden_audit_recovery_marker,
    verify_audit_recovery_marker_acl,
    verify_audit_storage_acl,
)
from .core import DecisionCode, ReasonCode


GENESIS_HASH = "0" * 64
_ENTRY_FIELDS = frozenset(
    {
        "event_id",
        "timestamp_utc",
        "policy_version",
        "decision_code",
        "reason_codes",
        "request_hash",
        "previous_hash",
        "entry_hash",
    }
)
_PENDING_SCHEMA = "obsidian-audit-pending-v1"
_PENDING_FIELDS = frozenset({"schema", "entry"})


class AuditRecoveryRequired(RuntimeError):
    """New audit decisions are blocked pending explicit recovery."""


class AuditRecoveryStatus(Enum):
    ENTRY_PRESENT = "entry_present"
    ENTRY_ABSENT = "entry_absent"
    CONFLICT = "conflict"


class AuditRecoveryAction(Enum):
    ACKNOWLEDGE_PRESENT = "acknowledge_present"
    APPEND_MISSING = "append_missing"


@dataclass(frozen=True, slots=True)
class AuditRecoveryReport:
    status: AuditRecoveryStatus
    event_id: str | None
    entry_hash: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class AuditEntry:
    event_id: str
    timestamp_utc: str
    policy_version: str
    decision_code: str
    reason_codes: tuple[str, ...]
    request_hash: str
    previous_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


def create_audit_entry(
    request: dict[str, object],
    assessment: dict[str, object],
    *,
    event_id: str,
    timestamp_utc: str,
    policy_version: str,
    previous_hash: str = GENESIS_HASH,
) -> AuditEntry:
    """Create one validated entry without writing it."""
    _validate_event_metadata(event_id, timestamp_utc, policy_version, previous_hash)
    if type(request) is not dict:
        raise ValueError("audit request must be a dictionary")
    if type(assessment) is not dict:
        raise ValueError("audit assessment must be a dictionary")
    decision_code = assessment.get("decision_code")
    reason_codes = assessment.get("reason_codes")
    if type(decision_code) is not str or decision_code not in {
        code.value for code in DecisionCode
    }:
        raise ValueError("audit decision_code is not registered")
    if type(reason_codes) is not list or any(
        type(code) is not str or code not in {item.value for item in ReasonCode}
        for code in reason_codes
    ):
        raise ValueError("audit reason_codes must be registered codes")

    request_hash = _hash_json(request)
    unsigned = {
        "decision_code": decision_code,
        "event_id": event_id,
        "policy_version": policy_version,
        "previous_hash": previous_hash,
        "reason_codes": reason_codes,
        "request_hash": request_hash,
        "timestamp_utc": timestamp_utc,
    }
    entry_hash = _hash_json(unsigned)
    return AuditEntry(
        event_id=event_id,
        timestamp_utc=timestamp_utc,
        policy_version=policy_version,
        decision_code=decision_code,
        reason_codes=tuple(reason_codes),
        request_hash=request_hash,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
    )


def append_audit_entry(path: Path, entry: AuditEntry) -> None:
    """Verify the existing chain and durably append one canonical JSON line."""
    if not isinstance(path, Path):
        raise ValueError("audit path must be a pathlib.Path")
    if type(entry) is not AuditEntry:
        raise ValueError("entry must be an AuditEntry")
    if not path.parent.is_dir():
        raise ValueError("audit parent directory must exist")
    ensure_audit_log_ready(path)
    entries = _read_and_verify(path)
    expected_previous = entries[-1].entry_hash if entries else GENESIS_HASH
    if entry.previous_hash != expected_previous:
        raise ValueError("audit previous_hash does not match the verified chain")
    _validate_entry(entry, expected_previous)

    _write_pending_intent(path, entry)
    _append_entry_line(path, entry)
    _remove_pending_intent(path)


def append_audit_assessment(
    path: Path,
    request: dict[str, object],
    assessment: dict[str, object],
    *,
    event_id: str,
    timestamp_utc: str,
    policy_version: str,
) -> AuditEntry:
    """Create and durably append one assessment to the verified chain."""
    ensure_audit_log_ready(path)
    entries = _read_and_verify(path)
    previous_hash = entries[-1].entry_hash if entries else GENESIS_HASH
    entry = create_audit_entry(
        request,
        assessment,
        event_id=event_id,
        timestamp_utc=timestamp_utc,
        policy_version=policy_version,
        previous_hash=previous_hash,
    )
    append_audit_entry(path, entry)
    return entry


def verify_audit_log(path: Path) -> bool:
    """Return true for a valid chain and raise ValueError for invalid data."""
    if not isinstance(path, Path):
        raise ValueError("audit path must be a pathlib.Path")
    verify_audit_storage_acl(path)
    _read_and_verify(path)
    return True


def ensure_audit_log_ready(path: Path) -> None:
    """Initialize storage and reject every unresolved recovery marker."""
    ensure_audit_storage_is_secure(path)
    marker = _pending_path(path)
    if marker.exists() or marker.is_symlink():
        verify_audit_recovery_marker_acl(path)
        raise AuditRecoveryRequired(
            "audit recovery is required before new policy decisions"
        )


def inspect_audit_recovery(path: Path) -> AuditRecoveryReport:
    """Classify a pending audit intent without changing either file."""
    marker = _pending_path(path)
    if not marker.exists() and not marker.is_symlink():
        raise ValueError("audit recovery marker does not exist")
    try:
        verify_audit_recovery_marker_acl(path)
        pending = _read_pending_intent(path)
    except (AuditAclError, OSError, ValueError) as exc:
        return AuditRecoveryReport(
            status=AuditRecoveryStatus.CONFLICT,
            event_id=None,
            entry_hash=None,
            detail=f"recovery data is invalid: {type(exc).__name__}",
        )
    try:
        verify_audit_storage_acl(path)
        entries = _read_and_verify(path)
    except (AuditAclError, OSError, ValueError) as exc:
        return AuditRecoveryReport(
            status=AuditRecoveryStatus.CONFLICT,
            event_id=pending.event_id,
            entry_hash=pending.entry_hash,
            detail=f"audit chain is invalid: {type(exc).__name__}",
        )

    matching = [entry for entry in entries if entry.entry_hash == pending.entry_hash]
    if len(matching) == 1 and entries[-1] == pending:
        return AuditRecoveryReport(
            status=AuditRecoveryStatus.ENTRY_PRESENT,
            event_id=pending.event_id,
            entry_hash=pending.entry_hash,
            detail="pending entry is the verified end of the audit chain",
        )
    previous_hash = entries[-1].entry_hash if entries else GENESIS_HASH
    if not matching and previous_hash == pending.previous_hash:
        return AuditRecoveryReport(
            status=AuditRecoveryStatus.ENTRY_ABSENT,
            event_id=pending.event_id,
            entry_hash=pending.entry_hash,
            detail="pending entry is absent and still matches the chain end",
        )
    return AuditRecoveryReport(
        status=AuditRecoveryStatus.CONFLICT,
        event_id=pending.event_id,
        entry_hash=pending.entry_hash,
        detail="pending entry conflicts with the verified audit chain",
    )


def resolve_audit_recovery(
    path: Path,
    *,
    expected_entry_hash: str,
    action: AuditRecoveryAction,
) -> None:
    """Explicitly resolve one inspected recovery state and unblock auditing."""
    if type(expected_entry_hash) is not str:
        raise ValueError("expected_entry_hash must be a string")
    if not isinstance(action, AuditRecoveryAction):
        raise ValueError("action must be an AuditRecoveryAction")
    report = inspect_audit_recovery(path)
    if report.entry_hash != expected_entry_hash:
        raise AuditRecoveryRequired("recovery entry hash does not match confirmation")
    if report.status is AuditRecoveryStatus.CONFLICT:
        raise AuditRecoveryRequired("conflicting audit recovery requires manual review")
    if report.status is AuditRecoveryStatus.ENTRY_PRESENT:
        if action is not AuditRecoveryAction.ACKNOWLEDGE_PRESENT:
            raise AuditRecoveryRequired("present recovery entry requires acknowledgement")
        # Readable bytes can still be cached after the original fsync failed.
        # Preserve the marker unless a new durability check succeeds.
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    elif report.status is AuditRecoveryStatus.ENTRY_ABSENT:
        if action is not AuditRecoveryAction.APPEND_MISSING:
            raise AuditRecoveryRequired("absent recovery entry requires explicit append")
        pending = _read_pending_intent(path)
        entries = _read_and_verify(path)
        previous_hash = entries[-1].entry_hash if entries else GENESIS_HASH
        _validate_entry(pending, previous_hash)
        _append_entry_line(path, pending)
        verified = _read_and_verify(path)
        if not verified or verified[-1] != pending:
            raise AuditRecoveryRequired("appended recovery entry did not verify")
    _remove_pending_intent(path)
    ensure_audit_log_ready(path)


def _write_pending_intent(path: Path, entry: AuditEntry) -> None:
    marker = _pending_path(path)
    if marker.exists() or marker.is_symlink():
        raise AuditRecoveryRequired("audit recovery marker already exists")
    payload = {
        "entry": entry.to_dict(),
        "schema": _PENDING_SCHEMA,
    }
    encoded = _canonical_json(payload).encode("utf-8") + b"\n"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        harden_audit_recovery_marker(path)
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError("incomplete audit recovery write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    flush_audit_directory(path)


def _read_pending_intent(path: Path) -> AuditEntry:
    marker = _pending_path(path)
    raw = marker.read_bytes()
    if not raw or not raw.endswith(b"\n") or len(raw.splitlines()) != 1:
        raise ValueError("audit recovery marker must contain one complete line")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("audit recovery marker is not valid JSON") from exc
    if type(value) is not dict or set(value) != _PENDING_FIELDS:
        raise ValueError("audit recovery marker schema is invalid")
    if value["schema"] != _PENDING_SCHEMA:
        raise ValueError("audit recovery marker version is invalid")
    entry = _entry_from_value(value["entry"], "audit recovery marker")
    _validate_entry(entry, entry.previous_hash)
    return entry


def _append_entry_line(path: Path, entry: AuditEntry) -> None:
    line = _canonical_json(entry.to_dict()).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, line)
        if written != len(line):
            raise OSError("incomplete audit write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_pending_intent(path: Path) -> None:
    marker = _pending_path(path)
    try:
        marker.unlink()
    except OSError as exc:
        raise AuditRecoveryRequired("cannot clear audit recovery marker") from exc
    if marker.exists() or marker.is_symlink():
        raise AuditRecoveryRequired("audit recovery marker remains after removal")


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".pending")


def _read_and_verify(path: Path) -> list[AuditEntry]:
    if not path.exists():
        return []
    if not path.is_file():
        raise ValueError("audit path must be a file")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("audit log has an incomplete final line")

    entries: list[AuditEntry] = []
    previous_hash = GENESIS_HASH
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid audit JSON at line {line_number}") from exc
        entry = _entry_from_value(value, f"line {line_number}")
        _validate_entry(entry, previous_hash)
        entries.append(entry)
        previous_hash = entry.entry_hash
    return entries


def _entry_from_value(value: object, location: str) -> AuditEntry:
    if type(value) is not dict or set(value) != _ENTRY_FIELDS:
        raise ValueError(f"invalid audit schema at {location}")
    if type(value["reason_codes"]) is not list:
        raise ValueError(f"invalid audit reason_codes at {location}")
    return AuditEntry(
        event_id=value["event_id"],
        timestamp_utc=value["timestamp_utc"],
        policy_version=value["policy_version"],
        decision_code=value["decision_code"],
        reason_codes=tuple(value["reason_codes"]),
        request_hash=value["request_hash"],
        previous_hash=value["previous_hash"],
        entry_hash=value["entry_hash"],
    )


def _validate_entry(entry: AuditEntry, expected_previous: str) -> None:
    _validate_event_metadata(
        entry.event_id,
        entry.timestamp_utc,
        entry.policy_version,
        entry.previous_hash,
    )
    if entry.previous_hash != expected_previous:
        raise ValueError("audit hash chain is broken")
    if type(entry.decision_code) is not str or entry.decision_code not in {
        code.value for code in DecisionCode
    }:
        raise ValueError("audit decision_code is not registered")
    if any(
        type(code) is not str or code not in {item.value for item in ReasonCode}
        for code in entry.reason_codes
    ):
        raise ValueError("audit reason_code is not registered")
    _require_hash(entry.request_hash, "request_hash")
    _require_hash(entry.entry_hash, "entry_hash")
    unsigned = entry.to_dict()
    del unsigned["entry_hash"]
    if _hash_json(unsigned) != entry.entry_hash:
        raise ValueError("audit entry hash does not match content")


def _validate_event_metadata(
    event_id: str,
    timestamp_utc: str,
    policy_version: str,
    previous_hash: str,
) -> None:
    if type(event_id) is not str:
        raise ValueError("event_id must be a canonical UUID")
    try:
        parsed_event_id = UUID(event_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("event_id must be a canonical UUID") from exc
    if str(parsed_event_id) != event_id:
        raise ValueError("event_id must be a canonical UUID")
    if type(timestamp_utc) is not str or not timestamp_utc.endswith("Z"):
        raise ValueError("timestamp_utc must be a UTC timestamp ending in Z")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp_utc[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp_utc must be a valid UTC timestamp") from exc
    if parsed_timestamp.tzinfo != timezone.utc or parsed_timestamp.microsecond:
        raise ValueError("timestamp_utc must use UTC whole-second precision")
    if type(policy_version) is not str or not policy_version.strip():
        raise ValueError("policy_version must be a non-empty string")
    _require_hash(previous_hash, "previous_hash")


def _require_hash(value: object, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 hash")


def _hash_json(value: object) -> str:
    try:
        encoded = _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("audit input must be canonical JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
'''
