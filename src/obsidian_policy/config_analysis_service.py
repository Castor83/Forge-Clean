"""Audited host facade for the fixed passive configuration analysis."""

from datetime import datetime
from pathlib import Path

from .config_analyzer import analyze_application_config as _analyze_application_config
from .decision_service import assess_and_record_request


CONFIG_ANALYSIS_CAPABILITY = "review_user_provided_configuration"


def run_audited_config_analysis(
    request: object,
    config: object,
    *,
    audit_path: Path,
    now_utc: datetime | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Audit the policy decision before releasing a fixed passive analysis."""
    assessment = assess_and_record_request(
        request,
        audit_path=audit_path,
        now_utc=now_utc,
    )
    if assessment["decision"] != "approved":
        return assessment, None
    if not _is_exact_config_analysis_request(request):
        raise ValueError("request is not bound to the configuration analysis capability")
    return assessment, _analyze_application_config(config)


def _is_exact_config_analysis_request(request: object) -> bool:
    return (
        type(request) is dict
        and request.get("capabilities") == [CONFIG_ANALYSIS_CAPABILITY]
    )
