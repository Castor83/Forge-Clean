"""Independent fresh-process check of the frozen generated audit modules."""

import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Mapping
from unittest.mock import patch

from src.obsidian_policy import decision_service
from .audit_acl_suite import _load_validated_acl
from .durable_audit_suite import _EXPECTED_TREE, _load_validated_storage


_CHILD_CHECK = '''import ast
import json
from pathlib import Path
import sys
from unittest.mock import patch
from src.obsidian_policy import decision_service
from src.forge_runtime.audit_acl_suite import _load_validated_acl
from src.forge_runtime.durable_audit_suite import _EXPECTED_TREE, _load_validated_storage

acl = _load_validated_acl(Path(sys.argv[1]).read_text(encoding="utf-8"))
tree = ast.parse(Path(sys.argv[2]).read_text(encoding="utf-8"))
if ast.dump(tree) != ast.dump(_EXPECTED_TREE):
    raise ValueError("fresh process requires the exact frozen audit module")
storage = _load_validated_storage(tree, acl)
path = Path(sys.argv[3])
report = storage.inspect_audit_recovery(path)
if report.status is not storage.AuditRecoveryStatus.ENTRY_PRESENT:
    raise AssertionError("fresh process did not observe the persisted marker")
if report.entry_hash != sys.argv[4]:
    raise AssertionError("fresh recovery confirmation hash changed")
with patch.object(decision_service, "ensure_audit_log_ready", storage.ensure_audit_log_ready), patch.object(decision_service, "_assess_request") as assess:
    try:
        decision_service.assess_and_record_request({}, audit_path=path)
    except storage.AuditRecoveryRequired:
        pass
    else:
        raise AssertionError("fresh process released a pending decision")
    assess.assert_not_called()
storage.resolve_audit_recovery(path, expected_entry_hash=sys.argv[4], action=storage.AuditRecoveryAction.ACKNOWLEDGE_PRESENT)
storage.ensure_audit_log_ready(path)
print(json.dumps({"initial_status": report.status.value, "policy_calls": assess.call_count, "resolved": True}))
'''


def verify_durable_audit_restart(contents: Mapping[str, str]) -> str | None:
    """Prove persisted blocking and explicit recovery using an owned fixture."""
    try:
        tree = ast.parse(contents["agent/audit.py"])
        if ast.dump(tree) != ast.dump(_EXPECTED_TREE):
            return "restart verifier requires the exact frozen audit module"
        acl = _load_validated_acl(contents["agent/audit_acl.py"])
        storage = _load_validated_storage(tree, acl)
        repository = Path(__file__).resolve().parents[2]
        with TemporaryDirectory(dir=repository) as directory:
            root = Path(directory)
            modules = root / "modules"
            modules.mkdir()
            acl_path = modules / "audit_acl.py"
            audit_path = modules / "audit.py"
            acl_path.write_bytes(contents["agent/audit_acl.py"].encode("utf-8"))
            audit_path.write_bytes(contents["agent/audit.py"].encode("utf-8"))
            state = root / "state"
            state.mkdir()
            path = state / "audit.jsonl"
            real_fsync = storage.os.fsync
            calls = 0

            def fail_second_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic original log fsync failure")
                real_fsync(descriptor)

            request = {
                "purpose": "Review synthetic restart fixture",
                "target_scope": "owned_system",
                "authorization_confirmed": True,
                "capabilities": ["review_user_provided_configuration"],
            }
            with patch.object(decision_service, "ensure_audit_log_ready", storage.ensure_audit_log_ready), patch.object(
                decision_service, "append_audit_assessment", storage.append_audit_assessment
            ), patch.object(storage.os, "fsync", side_effect=fail_second_sync):
                try:
                    decision_service.assess_and_record_request(
                        request, audit_path=path,
                        now_utc=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
                    )
                except OSError:
                    pass
                else:
                    raise AssertionError("fixture did not preserve failed sync state")
            report = storage.inspect_audit_recovery(path)
            if report.status is not storage.AuditRecoveryStatus.ENTRY_PRESENT:
                raise AssertionError("restart fixture has wrong recovery state")
            original = path.read_bytes()
            completed = subprocess.run(
                [sys.executable, "-c", _CHILD_CHECK, str(acl_path), str(audit_path), str(path), report.entry_hash],
                cwd=repository, capture_output=True, text=True, timeout=30,
            )
            if completed.returncode != 0:
                raise AssertionError(f"fresh process failed: {completed.stderr}")
            result = json.loads(completed.stdout)
            if result != {"initial_status": "entry_present", "policy_calls": 0, "resolved": True}:
                raise AssertionError("unexpected fresh-process recovery result")
            if storage._pending_path(path).exists() or path.read_bytes() != original:
                raise AssertionError("fresh recovery left marker or rewrote log")
            storage.verify_audit_log(path)
    except Exception as exc:
        return f"audit restart verification failed: {type(exc).__name__}: {exc}"
    return None
