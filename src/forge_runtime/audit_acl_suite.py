"""Fixed Windows audit ACL reproduction with isolated native acceptance."""

import __future__
import ast
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Mapping
from unittest.mock import Mock, patch

from src.obsidian_policy import audit, decision_service

from .audit_acl_contract import EXPECTED_SOURCE


_EXPECTED_TREE = ast.parse(EXPECTED_SOURCE)


def run_audit_acl(contents: Mapping[str, str]) -> str | None:
    """Validate the entire source before invoking its fixed native operations."""
    try:
        namespace = _load_validated_acl(contents["agent/audit_acl.py"])
        _run_acl_cases(namespace)
    except Exception as exc:
        return f"audit ACL host acceptance failed: {type(exc).__name__}: {exc}"
    return None


def _load_validated_acl(source: str):
    tree = ast.parse(source)
    if ast.dump(tree, include_attributes=False) != ast.dump(
        _EXPECTED_TREE, include_attributes=False
    ):
        raise ValueError("audit ACL must match the exact frozen Windows contract")
    namespace = {
        "__builtins__": {
            "isinstance": isinstance, "len": len, "str": str, "set": set,
            "OSError": OSError, "ValueError": ValueError, "RuntimeError": RuntimeError,
            "__build_class__": __build_class__,
        },
        "__name__": "forge_validated_audit_acl",
        "ctypes": ctypes,
        "wintypes": wintypes,
        "os": os,
        "Path": Path,
        "re": re,
    }
    # Imports are bindings chosen above, not generated executable statements.
    body = [node for node in tree.body if type(node) in (ast.Assign, ast.ClassDef, ast.FunctionDef)]
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, "<forge-audit-acl-suite>", "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


def _run_acl_cases(namespace):
    error = namespace["AuditAclError"]
    harden = namespace["harden_audit_storage"]
    verify = namespace["verify_audit_storage_acl"]
    ensure = namespace["ensure_audit_storage_is_secure"]
    harden_marker = namespace["harden_audit_recovery_marker"]
    verify_marker = namespace["verify_audit_recovery_marker_acl"]
    repository = Path(__file__).resolve().parents[2]

    def require(condition, detail):
        if not condition:
            raise AssertionError(detail)

    def expect_error(operation):
        try:
            operation()
        except error:
            return
        raise AssertionError("unsafe ACL state was accepted")

    with TemporaryDirectory(dir=repository) as directory:
        root = Path(directory)
        live = root / "live"
        live.mkdir()
        path = live / "audit.jsonl"
        harden(path)
        require(verify(path), "live storage DACL failed verification")
        marker = namespace["_recovery_marker_path"](path)
        marker.write_bytes(b"synthetic pending data")
        harden_marker(path)
        require(verify_marker(path), "live marker DACL failed verification")
        namespace["flush_audit_directory"](path)
        user_sid = namespace["_current_user_sid"]()
        real_read_owner = namespace["_read_owner_sid"]
        for target in (live, path, marker):
            require(real_read_owner(target) == user_sid, "live object owner did not match current user")

        # Inject an owner identity, never alter real Windows ownership. It
        # must block before any permission repair or overwrite is attempted.
        for target in (live, path, marker):
            setter = Mock()
            before = (path.read_bytes(), marker.read_bytes())
            with patch.dict(namespace, {
                "_read_owner_sid": lambda candidate: "S-1-5-21-11111-22222-33333-1234" if candidate == target else real_read_owner(candidate),
                "_set_strict_dacl": setter,
            }):
                if target == marker:
                    expect_error(lambda: harden_marker(path))
                    expect_error(lambda: verify_marker(path))
                else:
                    expect_error(lambda: harden(path))
                    expect_error(lambda: ensure(path))
                require(setter.call_count == 0, "foreign owner triggered ACL mutation")
            require(before == (path.read_bytes(), marker.read_bytes()), "foreign owner case changed data")

        with patch.dict(namespace, {"_read_owner_sid": lambda candidate: "S-1-5-18"}):
            require(verify(path), "SYSTEM owner was incorrectly rejected")

        # This deliberately broadens only a disposable synthetic test file.
        namespace["_set_dacl_sddl"](path, f"D:P(A;;FA;;;SY)(A;;FA;;;{user_sid})(A;;FR;;;WD)")
        broad_dacl = namespace["_read_dacl_sddl"](path)
        expect_error(lambda: ensure(path))
        require(namespace["_read_dacl_sddl"](path) == broad_dacl, "existing broad DACL was silently repaired")
        with patch.object(audit, "ensure_audit_storage_is_secure", ensure), patch.object(
            decision_service, "_assess_request"
        ) as assess:
            expect_error(lambda: decision_service.assess_and_record_request({}, audit_path=path))
            require(assess.call_count == 0, "broadened ACL reached policy evaluation")

        fresh = root / "fresh"
        fresh.mkdir()
        fresh_path = fresh / "audit.jsonl"
        with patch.dict(namespace, {"_set_strict_dacl": Mock(side_effect=error("synthetic set failure"))}):
            expect_error(lambda: harden(fresh_path))
        require(not fresh_path.exists(), "ACL set failure still created audit file")

        setter = Mock()
        with patch.dict(namespace, {
            "_read_owner_sid": Mock(side_effect=error("synthetic owner read failure")),
            "_set_strict_dacl": setter,
        }):
            expect_error(lambda: harden(fresh_path))
            require(setter.call_count == 0, "owner read failure changed ACL")

        unrelated = root / "unrelated"
        unrelated.mkdir()
        existing = unrelated / "preserve.txt"
        existing.write_bytes(b"preserve")
        setter = Mock()
        with patch.dict(namespace, {"_set_strict_dacl": setter}):
            expect_error(lambda: harden(unrelated / "audit.jsonl"))
            require(setter.call_count == 0, "unrelated directory was repermissioned")
        require(existing.read_bytes() == b"preserve", "unrelated data changed")
