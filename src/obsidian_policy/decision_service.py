"""Host-owned release boundary coupling policy decisions to durable audit."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .audit import append_audit_assessment, ensure_audit_log_ready
from .core import assess_request as _assess_request
from .execution_contract import ValidatedExecutionContract


POLICY_VERSION = "obsidian-policy-v1"


def assess_and_record_request(
    request: object,
    *,
    audit_path: Path,
    validated_contract: ValidatedExecutionContract | None = None,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Return a policy result only after its audit entry is durably appended."""
    if not isinstance(audit_path, Path):
        raise ValueError("audit_path must be a pathlib.Path")
    ensure_audit_log_ready(audit_path)
    decision_time = now_utc or _utc_now()
    assessment = _assess_request(
        request,
        validated_contract=validated_contract,
        now_utc=decision_time,
    )
    if type(request) is not dict:
        audited_request: dict[str, object] = {
            "invalid_request_type": type(request).__name__
        }
    else:
        audited_request = request
    append_audit_assessment(
        audit_path,
        audited_request,
        assessment,
        event_id=str(uuid4()),
        timestamp_utc=_format_utc(decision_time),
        policy_version=POLICY_VERSION,
    )
    return assessment


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _format_utc(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo != timezone.utc:
        raise ValueError("now_utc must be a timezone-aware UTC datetime")
    if value.microsecond:
        raise ValueError("now_utc must use whole-second precision")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
