"""Composed fixed acceptance of Forge-built audit storage and Windows ACLs."""

import ast
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Mapping
from unittest.mock import patch
from uuid import UUID

from src.obsidian_policy import decision_service
from src.obsidian_policy.core import DecisionCode, ReasonCode
from .audit_acl_suite import _load_validated_acl, _run_acl_cases
from .durable_audit_contract import EXPECTED_SOURCE
from .recovery_suite import _run_storage_cases


_EXPECTED_TREE = ast.parse(EXPECTED_SOURCE)


def run_durable_audit(contents: Mapping[str, str]) -> str | None:
    """Validate both modules before exposing native operations to fixed cases."""
    try:
        tree = ast.parse(contents["agent/audit.py"])
        if ast.dump(tree, include_attributes=False) != ast.dump(
            _EXPECTED_TREE, include_attributes=False
        ):
            return "durable audit must match the exact frozen storage contract"
        acl = _load_validated_acl(contents["agent/audit_acl.py"])
        _run_acl_cases(acl)
        storage = _load_validated_storage(tree, acl)
        with patch.object(decision_service, "ensure_audit_log_ready", storage.ensure_audit_log_ready), patch.object(
            decision_service, "append_audit_assessment", storage.append_audit_assessment
        ):
            _run_storage_cases(
                storage.__dict__, storage.os, storage=storage,
                assess=decision_service.assess_and_record_request,
            )
    except Exception as exc:
        return f"durable audit host acceptance failed: {type(exc).__name__}: {exc}"
    return None


def _load_validated_storage(tree, acl):
    """Called only after the complete immutable source contract matched."""
    storage = ModuleType("forge_validated_durable_audit")
    namespace = storage.__dict__
    namespace.update({
        "__builtins__": {
            "type": type, "str": str, "bool": bool, "len": len, "isinstance": isinstance,
            "any": any, "frozenset": frozenset, "dict": dict, "list": list,
            "set": set, "enumerate": enumerate,
            "tuple": tuple, "object": object, "ValueError": ValueError,
            "TypeError": TypeError, "OSError": OSError, "AttributeError": AttributeError,
            "UnicodeDecodeError": UnicodeDecodeError,
            "RuntimeError": RuntimeError, "__build_class__": __build_class__,
        },
        "hashlib": hashlib, "json": json, "os": os,
        "asdict": asdict, "dataclass": dataclass,
        "datetime": datetime, "timezone": timezone, "Enum": Enum,
        "Path": Path, "UUID": UUID,
        "DecisionCode": DecisionCode, "ReasonCode": ReasonCode,
    })
    acl_import = next(node for node in _EXPECTED_TREE.body if type(node) is ast.ImportFrom and node.module == "audit_acl")
    for alias in acl_import.names:
        namespace[alias.name] = acl[alias.name]
    body = [node for node in tree.body if type(node) in (ast.Assign, ast.ClassDef, ast.FunctionDef)]
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, "<forge-durable-audit-suite>", "exec"), namespace)
    return storage
