"""Closed, bounded project data; loading this data grants no host authority.

The read-only loader checks links and size before opening the selected file.
It does not prevent a different process from replacing paths between checks.
"""

import copy
import hashlib
import json
import re
from pathlib import Path

from .conversation_json import load_conversation_json


CONTEXT_FILENAME = "project_context.json"
MAX_CONTEXT_BYTES = 100_000
MAX_CONTEXT_TEXT = 16_000
MAX_RECORD_TEXT = 2_000
MAX_RECORDS = 64
MAX_REVISION = 1_000_000_000
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_FIELDS = {"version", "project_id", "revision", "rules", "decisions", "tasks",
           "next_step", "evidence"}
_DECISION_KEYS = {"objective", "acceptance_notes", "working_notes"}


def _identifier(value, field):
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Ungültige Projektkennung oder Kennung: " + field)


def _revision(value, maximum, field):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Ungültige oder zukünftige Revision: " + field)


def _text(value, field):
    if type(value) is not str or len(value) > MAX_RECORD_TEXT or not value.strip():
        raise ValueError("Leerer oder zu großer Projekttext: " + field)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Projekttext ist kein gültiges UTF-8: " + field) from exc
    return len(value)


def _records(data, field, fields):
    records = data[field]
    if type(records) is not list or len(records) > MAX_RECORDS:
        raise ValueError("Ungültige oder zu große Projektliste: " + field)
    for record in records:
        if type(record) is not dict or set(record) != fields:
            raise ValueError("Unbekannte oder fehlende Felder in Projektliste: " + field)
    return records


def _revision_history(records, key, revision):
    groups = {}
    total = 0
    text_field = "text" if key == "id" else "value"
    for record in records:
        if key == "id":
            _identifier(record[key], "rules.id")
        elif type(record[key]) is not str or record[key] not in _DECISION_KEYS:
            raise ValueError("Unbekannter strukturierter Entscheidungsschlüssel.")
        _revision(record["revision"], revision, key)
        if type(record["status"]) is not str or record["status"] not in {"active", "superseded"}:
            raise ValueError("Ungültiger Status einer Regel oder Entscheidung.")
        total += _text(record[text_field], text_field)
        groups.setdefault(record[key], []).append(record)
    for records_for_key in groups.values():
        revisions = [record["revision"] for record in records_for_key]
        active = [record for record in records_for_key if record["status"] == "active"]
        if len(set(revisions)) != len(revisions):
            raise ValueError("Mehrfach angegebene Revision einer Regel oder Entscheidung.")
        if len(active) != 1 or active[0]["revision"] != max(revisions):
            raise ValueError("Genau die neueste Revision muss die einzige aktive sein.")
    return total


def validate_project_context(data) -> None:
    """Validate all fields before any project data is consumed by the host."""
    if type(data) is not dict or set(data) != _FIELDS:
        raise ValueError("Unbekannte oder fehlende Felder im Projektkontext.")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unbekannte Projektkontextversion.")
    _identifier(data["project_id"], "project_id")
    _revision(data["revision"], MAX_REVISION, "revision")
    rules = _records(data, "rules", {"id", "revision", "status", "text"})
    decisions = _records(data, "decisions", {"key", "revision", "status", "value"})
    tasks = _records(data, "tasks", {"id", "status", "text"})
    evidence = _records(data, "evidence", {"id", "text"})
    total = _text(data["next_step"], "next_step")
    total += _revision_history(rules, "id", data["revision"])
    total += _revision_history(decisions, "key", data["revision"])
    for field, records in (("tasks", tasks), ("evidence", evidence)):
        identifiers = set()
        for record in records:
            _identifier(record["id"], field + ".id")
            if record["id"] in identifiers:
                raise ValueError("Mehrfach angegebene Kennung in Projektliste: " + field)
            identifiers.add(record["id"])
            total += _text(record["text"], field + ".text")
            if field == "tasks" and (type(record["status"]) is not str
                                       or record["status"] not in {"open", "done"}):
                raise ValueError("Ungültiger Aufgabenstatus.")
    if total > MAX_CONTEXT_TEXT:
        raise ValueError("Projekttexte überschreiten das gemeinsame Größenlimit.")


def fresh_project_context(project_id: str) -> dict:
    data = {"version": 1, "project_id": project_id, "revision": 1,
            "rules": [], "decisions": [], "tasks": [],
            "next_step": "Auftrag klären.", "evidence": []}
    validate_project_context(data)
    return data


def project_context_digest(data) -> str:
    """Bind to the entire validated context, including its historical records."""
    validate_project_context(data)
    content = json.dumps(data, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def effective_project_context(data) -> dict:
    """Return current project data, without promoting historical revisions.

    Caller-owned envelopes identify this as data. No stored text controls roles,
    tools, gates or the authority of this envelope. Evidence stays historical.
    """
    validate_project_context(data)
    effective = copy.deepcopy(data)
    for field in ("rules", "decisions"):
        effective[field] = [record for record in effective[field] if record["status"] == "active"]
    return effective


def _reject_links(path):
    # Include ancestors so an ordinary child of a linked directory is refused.
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("Projektkontextpfad darf keinen Link oder Junction enthalten.")


def load_project_context(directory: Path, expected_project_id: str | None = None) -> dict:
    """Read one selected context without creating, changing or repairing files."""
    if expected_project_id is not None:
        _identifier(expected_project_id, "expected_project_id")
    directory = Path(directory).absolute()
    _reject_links(directory)
    if not directory.is_dir():
        raise ValueError("Projektkontextordner fehlt oder ist kein Ordner.")
    path = directory / CONTEXT_FILENAME
    _reject_links(path)
    if not path.is_file():
        raise ValueError("Projektkontextdatei fehlt oder ist keine reguläre Datei.")
    if path.stat().st_size > MAX_CONTEXT_BYTES:
        raise ValueError("Projektkontextdatei ist zu groß.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CONTEXT_BYTES + 1)
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError("Projektkontextdatei ist zu groß.")
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Projektkontextdatei ist kein gültiges UTF-8.") from exc
    if content.startswith("\ufeff"):
        raise ValueError("Projektkontextdatei darf keine UTF-8-BOM enthalten.")
    data = load_conversation_json(content)
    validate_project_context(data)
    if expected_project_id is not None and data["project_id"] != expected_project_id:
        raise ValueError("Projektkontext gehört zu einer anderen Projektkennung.")
    return data
