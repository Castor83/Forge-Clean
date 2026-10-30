"""Operator CLI for fail-closed Obsidian audit recovery."""

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from src.obsidian_policy import (
    AuditAclError,
    AuditRecoveryAction,
    AuditRecoveryRequired,
    AuditRecoveryStatus,
    inspect_audit_recovery,
    resolve_audit_recovery,
    verify_audit_log,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    audit_path = Path(args.audit_path)
    if not audit_path.is_absolute():
        parser.error("--audit-path must be an absolute path")

    try:
        if args.command == "inspect":
            report = inspect_audit_recovery(audit_path)
            print(
                json.dumps(
                    {
                        "detail": report.detail,
                        "entry_hash": report.entry_hash,
                        "event_id": report.event_id,
                        "status": report.status.value,
                    },
                    sort_keys=True,
                )
            )
            if report.status is AuditRecoveryStatus.CONFLICT:
                return 3
            return 0

        if args.command == "resolve":
            action = AuditRecoveryAction(args.action)
            resolve_audit_recovery(
                audit_path,
                expected_entry_hash=args.expected_entry_hash,
                action=action,
            )
            print(
                json.dumps(
                    {
                        "action": action.value,
                        "entry_hash": args.expected_entry_hash,
                        "resolved": True,
                    },
                    sort_keys=True,
                )
            )
            return 0

        _verify_ready(audit_path)
        print(json.dumps({"audit_ready": True}, sort_keys=True))
        return 0
    except (AuditAclError, AuditRecoveryRequired, OSError, ValueError) as exc:
        print(f"audit recovery failed: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect, resolve, or verify Obsidian audit recovery state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="classify a pending marker without changing files",
    )
    _add_audit_path(inspect_parser)

    resolve_parser = subparsers.add_parser(
        "resolve",
        help="perform one explicit hash-bound recovery action",
    )
    _add_audit_path(resolve_parser)
    resolve_parser.add_argument(
        "--expected-entry-hash",
        required=True,
        help="exact entry_hash printed by the preceding inspect command",
    )
    resolve_parser.add_argument(
        "--action",
        required=True,
        choices=[action.value for action in AuditRecoveryAction],
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify the audit chain, ACL, and absence of a pending marker",
    )
    _add_audit_path(verify_parser)
    return parser


def _add_audit_path(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--audit-path",
        required=True,
        help="absolute path to audit.jsonl",
    )


def _verify_ready(audit_path: Path) -> None:
    """Verify operational readiness without creating or repairing files."""
    marker = audit_path.with_name(audit_path.name + ".pending")
    if marker.exists() or marker.is_symlink():
        raise AuditRecoveryRequired(
            "audit recovery is required before new policy decisions"
        )
    verify_audit_log(audit_path)


if __name__ == "__main__":
    raise SystemExit(main())
