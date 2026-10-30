"""Fixed verifier for durable JSONL persistence around the audit codec."""

import ast
import hashlib
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Mapping

from .audit_suite import run_minimized_audit_chain


_ENTRY_FIELDS = frozenset(
    {
        "event_id",
        "timestamp_utc",
        "policy_version",
        "decision_code",
        "reason_codes",
        "request_hash",
        "previous_hash",
        "entry_hash",
    }
)
_GENESIS_HASH = "0" * 64
_EXPECTED_FUNCTIONS = (
    ("append_audit_entry", ("path", "entry")),
    ("verify_audit_log", ("path",)),
    ("_read_entries", ("path",)),
    ("_canonical_json", ("value",)),
)
_ALLOWED_DIRECT_CALLS = frozenset(
    {
        "OSError",
        "ValueError",
        "_canonical_json",
        "_read_entries",
        "enumerate",
        "isinstance",
        "len",
        "list",
        "set",
        "type",
        "verify_audit_chain",
    }
)
_ALLOWED_OS_CALLS = frozenset({"close", "fsync", "open", "write"})
_ALLOWED_JSON_CALLS = frozenset({"dumps", "loads"})
_ALLOWED_METHOD_CALLS = frozenset(
    {"append", "decode", "encode", "endswith", "exists", "is_dir", "is_file", "read_bytes", "splitlines"}
)
_ALLOWED_NODES = (
    ast.Module,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Expr,
    ast.If,
    ast.IfExp,
    ast.For,
    ast.Try,
    ast.ExceptHandler,
    ast.Raise,
    ast.Return,
    ast.Break,
    ast.Pass,
    ast.Call,
    ast.keyword,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.BinOp,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.comprehension,
    ast.Add,
    ast.BitOr,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.Eq,
    ast.NotEq,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    ast.Load,
    ast.Store,
)


def run_jsonl_audit_log(contents: Mapping[str, str]) -> str | None:
    """Validate both modules, then exercise JSONL writes in a temp directory."""
    dependency_failure = run_minimized_audit_chain(
        {"agent/audit_chain.py": contents["agent/audit_chain.py"]}
    )
    if dependency_failure is not None:
        return f"audit dependency failed: {dependency_failure}"
    try:
        tree = ast.parse(contents["agent/audit_log.py"], filename="agent/audit_log.py")
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"
    failure = _validate_module(tree)
    if failure is not None:
        return failure
    return _run_cases(tree)


def _validate_module(tree: ast.Module) -> str | None:
    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    if len(body) != 8:
        return "audit log may contain only four imports and four functions"
    if not _imports_match(body[:4]):
        return "audit log imports do not match the fixed contract"
    functions = body[4:]
    if any(type(node) is not ast.FunctionDef for node in functions):
        return "audit log contains unexpected top-level code"
    failures: list[str] = []
    for function, (name, arguments) in zip(functions, _EXPECTED_FUNCTIONS):
        assert type(function) is ast.FunctionDef
        if not _signature_matches(function, name, arguments):
            failures.append(
                f"function {name} signature does not match the fixed contract"
            )
        failure = _validate_function(function)
        if failure is not None:
            failures.append(failure)
    if failures:
        return "; ".join(failures)
    if not _has_durable_append_shape(functions[0]):
        return "append must use O_APPEND, verify write length, fsync, and close in finally"
    return None


def _imports_match(nodes: list[ast.stmt]) -> bool:
    expected = (
        (ast.Import, None, (("json", None),)),
        (ast.Import, None, (("os", None),)),
        (ast.ImportFrom, "pathlib", (("Path", None),)),
        (
            ast.ImportFrom,
            "audit_chain",
            (
                ("ENTRY_FIELDS", None),
                ("GENESIS_HASH", None),
                ("verify_audit_chain", None),
            ),
        ),
    )
    for node, (node_type, module, names) in zip(nodes, expected):
        if type(node) is not node_type:
            return False
        if type(node) is ast.ImportFrom:
            expected_level = 1 if module == "audit_chain" else 0
            if node.module != module or node.level != expected_level:
                return False
        if tuple((alias.name, alias.asname) for alias in node.names) != names:
            return False
    return True


def _signature_matches(
    node: ast.FunctionDef,
    name: str,
    arguments: tuple[str, ...],
) -> bool:
    return (
        node.name == name
        and not node.decorator_list
        and node.returns is None
        and node.type_comment is None
        and not node.args.posonlyargs
        and node.args.vararg is None
        and not node.args.kwonlyargs
        and not node.args.kw_defaults
        and node.args.kwarg is None
        and not node.args.defaults
        and tuple(argument.arg for argument in node.args.args) == arguments
        and all(argument.annotation is None for argument in node.args.args)
    )


def _validate_function(node: ast.FunctionDef) -> str | None:
    failures: list[str] = []
    descendants = list(ast.walk(node))
    parents = {
        child: parent
        for parent in descendants
        for child in ast.iter_child_nodes(parent)
    }
    for descendant in descendants:
        if not isinstance(descendant, _ALLOWED_NODES):
            failures.append(_unsupported_syntax_failure(descendant, parents))
            continue
        if (
            type(descendant) is ast.ExceptHandler
            and not _is_exact_decode_error_handler(descendant)
        ):
            failures.append(
                f"line {descendant.lineno} except handler must catch exactly "
                "(UnicodeDecodeError, json.JSONDecodeError)"
            )
        if type(descendant) is ast.Constant and type(descendant.value) is str:
            if any(character in descendant.value for character in ("/", "\\")):
                failures.append(
                    f"line {descendant.lineno} contains a literal filesystem path"
                )
        if type(descendant) is ast.Attribute and descendant.attr.startswith("_"):
            failures.append(
                f"line {descendant.lineno} accesses a private attribute"
            )
        if type(descendant) is ast.Name and descendant.id in {
            "eval",
            "exec",
            "open",
            "compile",
            "__import__",
        }:
            failures.append(
                f"line {descendant.lineno} uses forbidden name {descendant.id}"
            )
        if type(descendant) is ast.Call and not _call_is_allowed(descendant):
            failures.append(
                f"line {descendant.lineno} call is outside the contract: "
                f"{ast.unparse(descendant)}"
            )
    for descendant in ast.walk(node):
        if (
            type(descendant) is ast.Name
            and descendant.id in {"path", "entry"}
            and type(descendant.ctx) is ast.Store
        ):
            failures.append(
                f"line {descendant.lineno} may not rebind {descendant.id}"
            )
    if failures:
        return "; ".join(dict.fromkeys(failures))
    return None


def _nearest_source_line(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> int:
    current = node
    while True:
        line = getattr(current, "lineno", None)
        if type(line) is int and line > 0:
            return line
        parent = parents.get(current)
        if parent is None:
            return 0
        current = parent


def _unsupported_syntax_failure(
    node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
) -> str:
    line = _nearest_source_line(node, parents)
    base = f"line {line} uses unsupported syntax {type(node).__name__}"
    comparison = _nearest_parent_of_type(node, parents, ast.Compare)
    if type(node) is not ast.Gt or comparison is None:
        return base
    expression = ast.unparse(comparison)
    if expression == "len(entries) > 0":
        return (
            f'{base} in "{expression}"; replace the if/else assignment with '
            'expected_previous = entries[-1]["entry_hash"] if entries else '
            "GENESIS_HASH"
        )
    if expression == "len(raw) > 0":
        return (
            f'{base} in "{expression}"; replace that condition with '
            'if raw and not raw.endswith(b"\\n"):'
        )
    return f'{base} in "{expression}"; rewrite the comparison without Gt'


def _nearest_parent_of_type(
    node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
    expected_type: type[ast.AST],
) -> ast.AST | None:
    current = parents.get(node)
    while current is not None:
        if type(current) is expected_type:
            return current
        current = parents.get(current)
    return None


def _is_exact_decode_error_handler(node: ast.ExceptHandler) -> bool:
    caught = node.type
    return (
        node.name is None
        and type(caught) is ast.Tuple
        and len(caught.elts) == 2
        and type(caught.elts[0]) is ast.Name
        and caught.elts[0].id == "UnicodeDecodeError"
        and type(caught.elts[1]) is ast.Attribute
        and type(caught.elts[1].value) is ast.Name
        and caught.elts[1].value.id == "json"
        and caught.elts[1].attr == "JSONDecodeError"
    )


def _call_is_allowed(node: ast.Call) -> bool:
    if type(node.func) is ast.Name:
        return node.func.id in _ALLOWED_DIRECT_CALLS
    if type(node.func) is not ast.Attribute:
        return False
    attribute = node.func.attr
    receiver = node.func.value
    if type(receiver) is ast.Name and receiver.id == "os":
        if attribute not in _ALLOWED_OS_CALLS:
            return False
        if attribute == "open":
            return bool(node.args) and type(node.args[0]) is ast.Name and node.args[0].id == "path"
        return True
    if type(receiver) is ast.Name and receiver.id == "json":
        return attribute in _ALLOWED_JSON_CALLS
    if attribute not in _ALLOWED_METHOD_CALLS:
        return False
    if attribute in {"exists", "is_file", "read_bytes"}:
        return type(receiver) is ast.Name and receiver.id == "path"
    if attribute == "is_dir":
        return (
            type(receiver) is ast.Attribute
            and receiver.attr == "parent"
            and type(receiver.value) is ast.Name
            and receiver.value.id == "path"
        )
    return True


def _has_durable_append_shape(function: ast.FunctionDef) -> bool:
    calls = [node for node in ast.walk(function) if type(node) is ast.Call]
    os_calls = [
        node
        for node in calls
        if type(node.func) is ast.Attribute
        and type(node.func.value) is ast.Name
        and node.func.value.id == "os"
    ]
    calls_by_name = {
        name: [node for node in os_calls if node.func.attr == name]
        for name in ("open", "write", "fsync", "close")
    }
    if any(len(nodes) != 1 for nodes in calls_by_name.values()):
        return False
    if len(os_calls) != 4:
        return False
    open_calls = [
        node
        for node in os_calls
        if node.func.attr == "open"
    ]
    opened = open_calls[0]
    if len(opened.args) != 3 or opened.keywords:
        return False
    flags = opened.args[1]
    flag_names = {
        node.attr
        for node in ast.walk(flags)
        if type(node) is ast.Attribute
        and type(node.value) is ast.Name
        and node.value.id == "os"
    }
    if flag_names != {"O_WRONLY", "O_CREAT", "O_APPEND"}:
        return False
    if type(opened.args[2]) is not ast.Constant or opened.args[2].value != 0o600:
        return False
    try_nodes = [node for node in ast.walk(function) if type(node) is ast.Try]
    if len(try_nodes) != 1:
        return False
    guarded = try_nodes[0]
    body_calls = [node for statement in guarded.body for node in ast.walk(statement)]
    final_calls = [
        node for statement in guarded.finalbody for node in ast.walk(statement)
    ]
    return (
        calls_by_name["write"][0] in body_calls
        and calls_by_name["fsync"][0] in body_calls
        and calls_by_name["close"][0] in final_calls
    )


def _run_cases(tree: ast.Module) -> str | None:
    functions = [node for node in tree.body if type(node) is ast.FunctionDef]
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    safe_globals = {
        "__builtins__": {
            "OSError": OSError,
            "UnicodeDecodeError": UnicodeDecodeError,
            "dict": dict,
            "ValueError": ValueError,
            "enumerate": enumerate,
            "isinstance": isinstance,
            "len": len,
            "list": list,
            "set": set,
            "type": type,
        },
        "ENTRY_FIELDS": _ENTRY_FIELDS,
        "GENESIS_HASH": _GENESIS_HASH,
        "Path": Path,
        "json": json,
        "os": os,
        "verify_audit_chain": _verify_chain,
    }
    exec(compile(module, "<forge-jsonl-audit-suite>", "exec"), safe_globals)
    append = safe_globals["append_audit_entry"]
    verify_log = safe_globals["verify_audit_log"]
    read_entries = safe_globals["_read_entries"]
    first = _make_entry(1, _GENESIS_HASH)
    second = _make_entry(2, first["entry_hash"])
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "audit.jsonl"
        try:
            append(path, first)
            append(path, second)
            if verify_log(path) is not True:
                failures.append("valid two-line audit log did not return exact True")
        except Exception as exc:
            failures.append(
                f"valid append and verify raised {type(exc).__name__}: {exc}"
            )
        if not path.exists():
            return "; ".join(failures)
        if path.exists():
            raw = path.read_bytes()
            lines = raw.splitlines()
            if not raw.endswith(b"\n") or len(lines) != 2:
                failures.append("audit log is not complete one-entry-per-line JSONL")
            else:
                expected = _canonical_json(first).encode("utf-8")
                if lines[0] != expected:
                    failures.append("audit line is not canonical JSON")
        original = path.read_bytes() if path.exists() else b""
        wrong = _make_entry(3, _GENESIS_HASH)
        try:
            append(path, wrong)
        except ValueError:
            pass
        except Exception as exc:
            failures.append(f"wrong previous hash raised {type(exc).__name__}")
        else:
            failures.append("wrong previous hash was appended")
        if path.read_bytes() != original:
            failures.append("rejected append changed the existing log")

        tampered = bytearray(original)
        if tampered:
            tampered[10] = ord("X")
            path.write_bytes(bytes(tampered))
            before = path.read_bytes()
            try:
                append(path, second)
            except ValueError:
                pass
            except Exception as exc:
                failures.append(f"tampered log raised {type(exc).__name__}")
            else:
                failures.append("append accepted a tampered existing log")
            if path.read_bytes() != before:
                failures.append("tampered-log rejection changed the file")

        path.write_bytes(_canonical_json(first).encode("utf-8"))
        try:
            verify_log(path)
        except ValueError:
            pass
        except Exception as exc:
            failures.append(f"incomplete final line raised {type(exc).__name__}")
        else:
            failures.append("incomplete final line was accepted")

        missing = root / "missing.jsonl"
        try:
            if read_entries(missing) != [] or verify_log(missing) is not True:
                failures.append("missing log is not treated as an empty valid chain")
        except Exception as exc:
            failures.append(f"missing log raised {type(exc).__name__}")
        try:
            append(root / "missing" / "audit.jsonl", first)
        except ValueError:
            pass
        except Exception as exc:
            failures.append(f"missing parent raised {type(exc).__name__}")
        else:
            failures.append("append accepted a missing parent directory")

        invalid_utf8 = root / "invalid-utf8.jsonl"
        invalid_utf8.write_bytes(b"\xff\n")
        try:
            verify_log(invalid_utf8)
        except ValueError:
            pass
        except Exception as exc:
            failures.append(f"invalid UTF-8 raised {type(exc).__name__}")
        else:
            failures.append("invalid UTF-8 was accepted")

        directory_path = root / "directory-log"
        directory_path.mkdir()
        try:
            verify_log(directory_path)
        except ValueError:
            pass
        except Exception as exc:
            failures.append(f"directory log raised {type(exc).__name__}")
        else:
            failures.append("directory path was accepted as an audit file")

        partial_path = root / "partial.jsonl"
        partial_os = _PartialWriteOS()
        safe_globals["os"] = partial_os
        try:
            append(partial_path, first)
        except OSError:
            pass
        except Exception as exc:
            failures.append(f"partial write raised {type(exc).__name__}")
        else:
            failures.append("partial write was accepted")
        finally:
            safe_globals["os"] = os
        if not partial_os.closed:
            failures.append("partial write did not close the descriptor")
        if partial_os.fsynced:
            failures.append("partial write was fsynced before rejection")

    copied = deepcopy(first)
    if copied != first:
        failures.append("host fixture changed unexpectedly")
    return "; ".join(failures) if failures else None


def _make_entry(index: int, previous_hash: str) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "decision_code": "REQUEST_APPROVED",
        "event_id": f"00000000-0000-4000-8000-{index:012d}",
        "policy_version": "policy-v1",
        "previous_hash": previous_hash,
        "reason_codes": ["REQUEST_WITHIN_DEFENSIVE_SCOPE"],
        "request_hash": hashlib.sha256(f"request-{index}".encode("ascii")).hexdigest(),
        "timestamp_utc": f"2026-09-26T12:00:0{index - 1}Z",
    }
    return dict(unsigned, entry_hash=_hash_json(unsigned))


class _PartialWriteOS:
    O_WRONLY = os.O_WRONLY
    O_CREAT = os.O_CREAT
    O_APPEND = os.O_APPEND

    def __init__(self) -> None:
        self.closed = False
        self.fsynced = False

    @staticmethod
    def open(path: Path, flags: int, mode: int) -> int:
        return os.open(path, flags, mode)

    @staticmethod
    def write(descriptor: int, data: bytes) -> int:
        return os.write(descriptor, data[:1])

    def fsync(self, descriptor: int) -> None:
        self.fsynced = True
        os.fsync(descriptor)

    def close(self, descriptor: int) -> None:
        self.closed = True
        os.close(descriptor)


def _verify_chain(entries: object) -> bool:
    if type(entries) is not list:
        raise ValueError("entries must be an exact list")
    previous_hash = _GENESIS_HASH
    for entry in entries:
        if type(entry) is not dict or set(entry) != _ENTRY_FIELDS:
            raise ValueError("invalid entry schema")
        if entry["previous_hash"] != previous_hash:
            raise ValueError("broken audit chain")
        unsigned = {key: value for key, value in entry.items() if key != "entry_hash"}
        if _hash_json(unsigned) != entry["entry_hash"]:
            raise ValueError("audit hash mismatch")
        previous_hash = entry["entry_hash"]
    return True


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_docstring(node: ast.AST) -> bool:
    return (
        type(node) is ast.Expr
        and type(node.value) is ast.Constant
        and type(node.value.value) is str
    )
