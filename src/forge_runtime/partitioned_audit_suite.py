"""Verify small inert source fragments before composing the fixed modules."""

from typing import Mapping
from itertools import zip_longest

from .audit_acl_contract import EXPECTED_SOURCE as ACL_SOURCE
from .audit_acl_suite import run_audit_acl
from .durable_audit_contract import EXPECTED_SOURCE as AUDIT_SOURCE
from .durable_audit_suite import run_durable_audit


def partition_source(source: str) -> tuple[str, ...]:
    parts: list[str] = []
    current = ""
    for line in source.splitlines(keepends=True):
        if len(current) + len(line) > 2200 and current:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return tuple(parts)


ACL_PARTS = partition_source(ACL_SOURCE)
AUDIT_PARTS = partition_source(AUDIT_SOURCE)
ACL_PART_FILES = tuple(f"parts/acl_{index}.txt" for index in range(1, len(ACL_PARTS) + 1))
AUDIT_PART_FILES = tuple(f"parts/audit_{index}.txt" for index in range(1, len(AUDIT_PARTS) + 1))


def _compose(contents, paths, expected):
    for index, (path, fragment) in enumerate(zip(paths, expected), start=1):
        if contents[path] != fragment:
            expected_lines = fragment.splitlines(keepends=True)
            actual_lines = contents[path].splitlines(keepends=True)
            for line, (expected_line, actual_line) in enumerate(
                zip_longest(expected_lines, actual_lines), start=1
            ):
                if expected_line != actual_line:
                    start = max(0, line - 2)
                    expected_context = "".join(expected_lines[start:line + 2])
                    actual_context = "".join(actual_lines[start:line + 2])
                    return None, (
                        f"source block {index} ({path}) must match the exact frozen fragment; "
                        f"first differing line {line}: expected {repr(expected_line)[:240]}, "
                        f"actual {repr(actual_line)[:240]}; "
                        f"expected surrounding text {repr(expected_context)[:400]}, "
                        f"actual surrounding text {repr(actual_context)[:400]}. "
                        "Preserve required surrounding lines when inserting a missing line."
                    )
    return "".join(contents[path] for path in paths), None


def run_partitioned_audit_acl(contents: Mapping[str, str]) -> str | None:
    source, failure = _compose(contents, ACL_PART_FILES, ACL_PARTS)
    if failure is not None:
        return failure
    return run_audit_acl({"agent/audit_acl.py": source})


def run_partitioned_durable_audit(contents: Mapping[str, str]) -> str | None:
    source, failure = _compose(contents, AUDIT_PART_FILES, AUDIT_PARTS)
    if failure is not None:
        return failure
    return run_durable_audit({
        "agent/audit.py": source,
        "agent/audit_acl.py": contents["agent/audit_acl.py"],
    })
