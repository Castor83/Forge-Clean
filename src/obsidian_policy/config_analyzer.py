"""Pure, bounded analysis of synthetic application configuration data."""

EXPECTED_FIELDS = frozenset(
    {
        "debug",
        "tls_enabled",
        "logging_enabled",
        "allowed_hosts",
        "admin_interface_public",
    }
)

OPTIONAL_REDACTED_FIELDS = frozenset(
    {"api_token", "database_password", "private_key"}
)
REDACTION_MARKER = "REDACTED"

SENSITIVE_KEY_PARTS = ("password", "secret", "token", "private_key", "api_key")
MAX_ALLOWED_HOSTS = 20
MAX_HOST_LENGTH = 253


def analyze_application_config(config: object) -> dict[str, object]:
    """Analyze a small in-memory configuration without performing any I/O."""
    redaction_reasons = _validate_redaction_contract(config)
    if redaction_reasons:
        return {
            "decision": "rejected",
            "findings": [],
            "reasons": redaction_reasons,
        }

    validation_reasons = _validate_config(config)
    if validation_reasons:
        return {
            "decision": "needs_clarification",
            "findings": [],
            "reasons": validation_reasons,
        }

    assert type(config) is dict
    findings: list[dict[str, str]] = []

    if config["debug"] is True:
        findings.append(
            _finding(
                "debug_enabled",
                "medium",
                "debug",
                "Debug mode is enabled.",
                "Disable debug mode outside an isolated development environment.",
            )
        )
    if config["tls_enabled"] is False:
        findings.append(
            _finding(
                "tls_disabled",
                "high",
                "tls_enabled",
                "TLS is disabled.",
                "Enable TLS before transmitting data over a network.",
            )
        )
    if config["logging_enabled"] is False:
        findings.append(
            _finding(
                "security_logging_disabled",
                "medium",
                "logging_enabled",
                "Security-relevant logging is disabled.",
                "Enable appropriate security logging without recording secrets.",
            )
        )
    if "*" in config["allowed_hosts"]:
        findings.append(
            _finding(
                "wildcard_allowed_host",
                "high",
                "allowed_hosts",
                "The allowed host list contains a wildcard.",
                "Replace the wildcard with the exact required host names.",
            )
        )
    if config["admin_interface_public"] is True:
        findings.append(
            _finding(
                "public_admin_interface",
                "high",
                "admin_interface_public",
                "The administration interface is marked as public.",
                "Restrict the administration interface to an authorized network.",
            )
        )

    return {
        "decision": "completed",
        "findings": findings,
        "reasons": [
            "analysis is limited to the fixed synthetic configuration rules"
        ],
    }


def _validate_config(config: object) -> list[str]:
    if type(config) is not dict:
        return ["config must be a dictionary"]

    reasons: list[str] = []
    keys = list(config.keys())
    non_string_keys = [key for key in keys if type(key) is not str]
    if non_string_keys:
        reasons.append("all config field names must be strings")

    string_keys = [key for key in keys if type(key) is str]
    unknown_sensitive_keys = sorted(
        key
        for key in string_keys
        if key not in OPTIONAL_REDACTED_FIELDS
        if any(part in key.casefold() for part in SENSITIVE_KEY_PARTS)
    )
    if unknown_sensitive_keys:
        reasons.extend(
            f"sensitive field must be removed before analysis: {key}"
            for key in unknown_sensitive_keys
        )

    actual_fields = set(string_keys)
    missing = sorted(EXPECTED_FIELDS - actual_fields)
    unknown = sorted(actual_fields - EXPECTED_FIELDS - OPTIONAL_REDACTED_FIELDS)
    reasons.extend(f"missing field: {field}" for field in missing)
    reasons.extend(f"unknown field: {field}" for field in unknown)

    for field in ("debug", "tls_enabled", "logging_enabled", "admin_interface_public"):
        if field in config and type(config[field]) is not bool:
            reasons.append(f"{field} must be a boolean")

    if "allowed_hosts" in config:
        allowed_hosts = config["allowed_hosts"]
        if type(allowed_hosts) is not list:
            reasons.append("allowed_hosts must be a list")
        elif len(allowed_hosts) > MAX_ALLOWED_HOSTS:
            reasons.append(f"allowed_hosts must contain at most {MAX_ALLOWED_HOSTS} items")
        else:
            for index, host in enumerate(allowed_hosts):
                if type(host) is not str or not host.strip():
                    reasons.append(
                        f"allowed_hosts item at index {index} must be a non-empty string"
                    )
                elif len(host) > MAX_HOST_LENGTH:
                    reasons.append(
                        f"allowed_hosts item at index {index} exceeds {MAX_HOST_LENGTH} characters"
                    )

    return reasons


def _validate_redaction_contract(config: object) -> list[str]:
    if type(config) is not dict:
        return []
    violations = sorted(
        field
        for field in OPTIONAL_REDACTED_FIELDS
        if field in config and config[field] != REDACTION_MARKER
    )
    return [
        f"sensitive field must contain exact REDACTED marker: {field}"
        for field in violations
    ]


def _finding(
    rule_id: str,
    severity: str,
    setting: str,
    message: str,
    recommendation: str,
) -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "setting": setting,
        "message": message,
        "recommendation": recommendation,
    }
