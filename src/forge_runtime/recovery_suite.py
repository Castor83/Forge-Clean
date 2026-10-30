"""Fixed AST and isolated real-storage acceptance for recovery orchestration."""

import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Mapping
from unittest.mock import patch

from src.obsidian_policy import audit
from src.obsidian_policy.decision_service import assess_and_record_request


_EXPECTED_SOURCE = '''import os
from .audit import (
    AuditAclError, AuditRecoveryAction, AuditRecoveryReport,
    AuditRecoveryRequired, AuditRecoveryStatus, GENESIS_HASH,
    _pending_path, _read_pending_intent, _read_and_verify,
    _validate_entry, _append_entry_line, _remove_pending_intent,
    ensure_audit_log_ready, verify_audit_recovery_marker_acl,
    verify_audit_storage_acl,
)

def inspect_audit_recovery(path):
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

def resolve_audit_recovery(path, *, expected_entry_hash, action):
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
'''
_EXPECTED_TREE = ast.parse(_EXPECTED_SOURCE)


def run_audit_recovery(contents: Mapping[str, str]) -> str | None:
    """Execute only the fixed validated functions, using host-owned storage."""
    try:
        tree = ast.parse(contents["agent/audit_recovery.py"])
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"
    if ast.dump(tree, include_attributes=False) != ast.dump(
        _EXPECTED_TREE, include_attributes=False
    ):
        return "recovery must use the exact inspection and durable resolution structure"

    # Never execute generated imports or arbitrary statements. All injected
    # operations and temporary storage paths are selected by this host suite.
    safe_os = SimpleNamespace(
        open=os.open, fsync=os.fsync, close=os.close,
        O_WRONLY=os.O_WRONLY, O_APPEND=os.O_APPEND,
    )
    namespace = {
        "__builtins__": {
            "type": type, "str": str, "isinstance": isinstance, "len": len,
            "ValueError": ValueError, "OSError": OSError,
        },
        "os": safe_os,
    }
    for alias in _EXPECTED_TREE.body[1].names:
        namespace[alias.name] = getattr(audit, alias.name)
    module = ast.fix_missing_locations(ast.Module(body=tree.body[2:], type_ignores=[]))
    exec(compile(module, "<forge-recovery-suite>", "exec"), namespace)
    try:
        _run_storage_cases(namespace, safe_os)
    except Exception as exc:
        return f"recovery host case failed: {type(exc).__name__}: {exc}"
    return None


def _run_storage_cases(
    namespace, safe_os, *, storage=audit, assess=assess_and_record_request
):
    # Defaults verify the narrow recovery build against reference storage;
    # the complete storage suite injects both Forge-built storage and ACLs.
    audit = storage
    assess_and_record_request = assess
    inspect = namespace["inspect_audit_recovery"]
    resolve = namespace["resolve_audit_recovery"]
    present = audit.AuditRecoveryStatus.ENTRY_PRESENT
    absent = audit.AuditRecoveryStatus.ENTRY_ABSENT
    acknowledge = audit.AuditRecoveryAction.ACKNOWLEDGE_PRESENT
    append = audit.AuditRecoveryAction.APPEND_MISSING
    request = {
        "purpose": "Review synthetic configuration",
        "target_scope": "owned_system",
        "authorization_confirmed": True,
        "capabilities": ["review_user_provided_configuration"],
    }
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    repository = Path(__file__).resolve().parents[2]

    def require(condition, detail):
        if not condition:
            raise AssertionError(detail)

    def expect_error(exception, operation):
        try:
            operation()
        except exception:
            return
        raise AssertionError(f"expected {exception.__name__}")

    def prepare(path, status):
        path.parent.mkdir()
        if status is present:
            real_fsync = os.fsync
            calls = 0

            def fail_second_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic original log sync failure")
                real_fsync(descriptor)

            with patch.object(audit.os, "fsync", side_effect=fail_second_sync):
                expect_error(OSError, lambda: assess_and_record_request(request, audit_path=path, now_utc=now))
        else:
            with patch.object(audit, "_append_entry_line", side_effect=OSError("synthetic append failure")):
                expect_error(OSError, lambda: assess_and_record_request(request, audit_path=path, now_utc=now))
        marker = audit._pending_path(path)
        before = (path.read_bytes(), marker.read_bytes())
        report = inspect(path)
        require(report.status is status, "inspection classified the wrong state")
        require(before == (path.read_bytes(), marker.read_bytes()), "inspection changed storage")
        return marker, report

    with TemporaryDirectory(dir=repository) as directory:
        for index, status in enumerate((present, absent)):
            path = Path(directory) / f"case-{index}" / "audit.jsonl"
            marker, report = prepare(path, status)
            original = (path.read_bytes(), marker.read_bytes())
            correct = acknowledge if status is present else append
            wrong = append if status is present else acknowledge
            for operation in (
                lambda: resolve(path, expected_entry_hash="0" * 64, action=correct),
                lambda: resolve(path, expected_entry_hash=report.entry_hash, action=wrong),
            ):
                expect_error(audit.AuditRecoveryRequired, operation)
                require(original == (path.read_bytes(), marker.read_bytes()), "rejected recovery changed storage")

            if status is present:
                descriptors = []

                def fail_recovery_sync(descriptor):
                    descriptors.append(descriptor)
                    raise OSError("synthetic recovery sync failure")

                with patch.object(safe_os, "fsync", side_effect=fail_recovery_sync):
                    expect_error(OSError, lambda: resolve(path, expected_entry_hash=report.entry_hash, action=correct))
                require(len(descriptors) == 1, "present recovery did not retry sync")
                expect_error(OSError, lambda: os.fstat(descriptors[0]))
                require(original == (path.read_bytes(), marker.read_bytes()), "sync failure cleared or changed marker")
                expect_error(audit.AuditRecoveryRequired, lambda: audit.ensure_audit_log_ready(path))
            else:
                with patch.dict(namespace, {"_append_entry_line": lambda *args: None}):
                    expect_error(audit.AuditRecoveryRequired, lambda: resolve(path, expected_entry_hash=report.entry_hash, action=correct))
                require(original == (path.read_bytes(), marker.read_bytes()), "unverified append cleared marker")

            resolve(path, expected_entry_hash=report.entry_hash, action=correct)
            require(not marker.exists(), "successful recovery did not clear marker")
            require(audit.verify_audit_log(path), "recovered audit chain is invalid")
            require(len(path.read_bytes().splitlines()) == 1, "recovery duplicated or lost entry")
            if status is present:
                require(path.read_bytes() == original[0], "acknowledgement rewrote existing entry")
            assess_and_record_request(request, audit_path=path, now_utc=now)
            require(len(path.read_bytes().splitlines()) == 2, "new policy decision did not resume")
            expect_error(ValueError, lambda: inspect(path))

            # A valid entry already inside the chain must not be treated as
            # recoverable when it is no longer the chain's final entry.
            first_entry = audit._read_and_verify(path)[0]
            audit._write_pending_intent(path, first_entry)
            original = (path.read_bytes(), marker.read_bytes())
            require(inspect(path).status is audit.AuditRecoveryStatus.CONFLICT, "non-final pending entry did not become conflict")
            expect_error(audit.AuditRecoveryRequired, lambda: resolve(path, expected_entry_hash=first_entry.entry_hash, action=acknowledge))
            require(original == (path.read_bytes(), marker.read_bytes()), "non-final conflict mutated storage")

        conflict_path = Path(directory) / "conflict" / "audit.jsonl"
        marker, report = prepare(conflict_path, absent)
        conflict_path.write_bytes(b"incomplete")
        original = (conflict_path.read_bytes(), marker.read_bytes())
        require(inspect(conflict_path).status is audit.AuditRecoveryStatus.CONFLICT, "invalid log did not become conflict")
        for action in (acknowledge, append):
            expect_error(audit.AuditRecoveryRequired, lambda: resolve(conflict_path, expected_entry_hash=report.entry_hash, action=action))
        require(original == (conflict_path.read_bytes(), marker.read_bytes()), "conflict recovery mutated storage")

        malformed_path = Path(directory) / "malformed-marker" / "audit.jsonl"
        marker, report = prepare(malformed_path, absent)
        value = json.loads(marker.read_bytes())
        value["entry"]["decision_code"] = []
        marker.write_text(json.dumps(value) + "\n", encoding="utf-8")
        original = (malformed_path.read_bytes(), marker.read_bytes())
        require(inspect(malformed_path).status is audit.AuditRecoveryStatus.CONFLICT, "malformed marker did not become conflict")
        expect_error(audit.AuditRecoveryRequired, lambda: resolve(malformed_path, expected_entry_hash=report.entry_hash, action=append))
        require(original == (malformed_path.read_bytes(), marker.read_bytes()), "malformed marker was changed by recovery")
        for invalid_bytes in (b"{invalid}\n", b"\xff\n"):
            marker.write_bytes(invalid_bytes)
            before = (malformed_path.read_bytes(), marker.read_bytes())
            require(inspect(malformed_path).status is audit.AuditRecoveryStatus.CONFLICT, "undecodable marker did not become conflict")
            require(before == (malformed_path.read_bytes(), marker.read_bytes()), "invalid marker inspection changed storage")
            conflict_path.write_bytes(invalid_bytes)
            require(inspect(conflict_path).status is audit.AuditRecoveryStatus.CONFLICT, "undecodable audit log did not become conflict")
