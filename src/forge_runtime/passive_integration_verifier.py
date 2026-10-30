"""Fixed native integration proof for four previously accepted Forge modules."""

import ast
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping
from unittest.mock import patch

from src.obsidian_policy import decision_service
from .audit_acl_suite import _load_validated_acl
from .audited_config_analysis_suite import _EXPECTED_TREE as FACADE_TREE
from .config_analysis_suite import (
    _load_validated_analysis, _validate_safe_structure, run_redacted_config_analysis,
)
from .durable_audit_suite import (
    _EXPECTED_TREE as AUDIT_TREE, _load_validated_storage, run_durable_audit,
)


def verify_passive_integration(contents: Mapping[str, str]) -> str | None:
    """Accept fixed ASTs before executing the owned passive test fixtures."""
    try:
        facade_tree = ast.parse(contents["agent/config_analysis_service.py"])
        if ast.dump(facade_tree) != ast.dump(FACADE_TREE):
            return "passive integration requires the exact frozen facade"
        audit_tree = ast.parse(contents["agent/audit.py"])
        if ast.dump(audit_tree) != ast.dump(AUDIT_TREE):
            return "passive integration requires the exact frozen audit storage"
        analyzer_tree = ast.parse(contents["agent/config_analyzer.py"])
        failure = _validate_safe_structure(analyzer_tree)
        if failure is not None:
            return failure
        acl = _load_validated_acl(contents["agent/audit_acl.py"])
        for verifier in (run_redacted_config_analysis, run_durable_audit):
            failure = verifier(contents)
            if failure is not None:
                return failure
        storage = _load_validated_storage(audit_tree, acl)
        analyzer = _load_validated_analysis(analyzer_tree)["analyze_application_config"]
        _run_passive_cases(facade_tree, storage, analyzer)
    except Exception as exc:
        return f"passive integration failed: {type(exc).__name__}: {exc}"
    return None


def _run_passive_cases(facade_tree, storage, analyzer, *, service=decision_service):
    decision_service = service
    request = {
        "purpose": "Review owned synthetic application configuration",
        "target_scope": "owned_system",
        "authorization_confirmed": True,
        "capabilities": ["review_user_provided_configuration"],
    }
    config = {
        "debug": True, "tls_enabled": True, "logging_enabled": True,
        "allowed_hosts": ["app.test"], "admin_interface_public": False,
    }
    calls = []
    current_path = None

    def checked_analyzer(value):
        # Observe real persisted storage at the instant analysis is reached.
        calls.append(None)
        storage.ensure_audit_log_ready(current_path)
        storage.verify_audit_log(current_path)
        entries = storage._read_and_verify(current_path)
        if not entries or entries[-1].decision_code != "REQUEST_APPROVED":
            raise AssertionError("analyzer reached without a persisted approval")
        calls[-1] = len(entries)
        return analyzer(value)

    namespace = {
        "__builtins__": {"type": type, "dict": dict, "ValueError": ValueError},
        "assess_and_record_request": decision_service.assess_and_record_request,
        "analyze_application_config": checked_analyzer,
    }
    function = facade_tree.body[-1]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, "<forge-passive-integration>", "exec"), namespace)
    gate = namespace["run_audited_config_analysis"]

    def expect_error(exception, operation):
        try:
            operation()
        except exception:
            return
        raise AssertionError(f"expected {exception.__name__}")

    def require(condition, detail):
        if not condition:
            raise AssertionError(detail)

    repository = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=repository) as directory, patch.object(
        decision_service, "ensure_audit_log_ready", storage.ensure_audit_log_ready
    ), patch.object(decision_service, "append_audit_assessment", storage.append_audit_assessment):
        root = Path(directory)

        def case(name):
            nonlocal current_path
            calls.clear()
            current_path = root / name / "audit.jsonl"
            current_path.parent.mkdir()
            return current_path

        path = case("approved")
        original = deepcopy((request, config))
        assessment, analysis = gate(request, config, path)
        require(assessment["decision"] == "approved", "valid request was not approved")
        require(analysis["decision"] == "completed" and calls == [1], "real analysis did not follow durable approval")
        require(analysis["findings"][0]["rule_id"] == "debug_enabled", "actual analyzer result missing")
        require((request, config) == original, "integration modified caller data")

        for name, changes, expected in (
            ("prohibited", {"capabilities": ["obtain_credentials"]}, "rejected"),
            ("unauthorized", {"authorization_confirmed": False}, "needs_clarification"),
        ):
            path = case(name)
            assessment, analysis = gate({**request, **changes}, config, path)
            require(assessment["decision"] == expected and analysis is None and not calls, "denied request reached analyzer")
            require(len(storage._read_and_verify(path)) == 1, "denied decision was not audited")

        path = case("wrong-capability")
        expect_error(ValueError, lambda: gate({**request, "capabilities": ["explain_security_concepts"]}, config, path))
        require(not calls, "unbound passive capability reached analyzer")

        path = case("write-failure")
        real_write = storage.os.write
        write_calls = 0

        def fail_log_write(descriptor, data):
            nonlocal write_calls
            write_calls += 1
            if write_calls == 2:
                raise OSError("synthetic audit append failure")
            return real_write(descriptor, data)

        with patch.object(storage.os, "write", side_effect=fail_log_write):
            expect_error(OSError, lambda: gate(request, config, path))
        require(not calls and storage._pending_path(path).exists(), "write failure released analysis or lost marker")
        require(storage.inspect_audit_recovery(path).status is storage.AuditRecoveryStatus.ENTRY_ABSENT, "write failure fixture has wrong state")

        path = case("sync-failure-and-recovery")
        real_fsync = storage.os.fsync
        sync_calls = 0

        def fail_log_sync(descriptor):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                raise OSError("synthetic audit durability failure")
            real_fsync(descriptor)

        with patch.object(storage.os, "fsync", side_effect=fail_log_sync):
            expect_error(OSError, lambda: gate(request, config, path))
        require(not calls, "sync failure reached analyzer")
        with patch.object(decision_service, "_assess_request") as policy:
            expect_error(storage.AuditRecoveryRequired, lambda: gate(request, config, path))
            policy.assert_not_called()
        require(not calls, "pending recovery reached analyzer")
        report = storage.inspect_audit_recovery(path)
        storage.resolve_audit_recovery(path, expected_entry_hash=report.entry_hash, action=storage.AuditRecoveryAction.ACKNOWLEDGE_PRESENT)
        assessment, analysis = gate(request, config, path)
        require(calls == [2] and analysis["decision"] == "completed", "analysis did not resume after explicit recovery")

        path = case("corrupt-chain")
        gate(request, config, path)
        calls.clear()
        path.write_bytes(b"corrupt synthetic chain\n")
        expect_error(ValueError, lambda: gate(request, config, path))
        require(not calls, "corrupted chain reached analyzer")

        path = case("unredacted-config")
        secret_config = {**config, "database_password": "synthetic-private-value"}
        assessment, analysis = gate(request, secret_config, path)
        require(calls == [1] and analysis["decision"] == "rejected", "real analyzer did not enforce redaction")
        require(b"synthetic-private-value" not in path.read_bytes(), "configuration secret reached policy audit")
