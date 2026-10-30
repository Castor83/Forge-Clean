"""Versioned execution snapshots, separate from Forge's conversation state."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


TASK_FIELDS = frozenset({"schema_version", "task_id", "objective", "constraints",
                         "model_id", "status", "attempt_count", "max_attempts",
                         "created_at", "updated_at", "started_at", "completed_at",
                         "result", "error"})
TASK_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
MAX_JSON_DEPTH = 64
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DEVICES = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}


def validate_task_id(value):
    """An ID is one portable filename component, never a path or device."""
    if (type(value) is not str or _ID_PATTERN.fullmatch(value) is None
            or value.upper() in _DEVICES):
        raise ValueError("Task-ID muss ein sicherer Dateiname mit 1 bis 64 Zeichen sein.")
    return value


def _text(value, name):
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} muss ein nichtleerer Text sein.")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"{name} muss gültiger UTF-8-Text sein.") from exc


def _json_snapshot(value, ancestors=None, depth=0):
    """Copy only finite JSON values, refusing cycles and unbounded recursion."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Task-JSON ist zu tief verschachtelt.")
    kind = type(value)
    if value is None or kind in (bool, int):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise ValueError("Task-JSON darf nur endliche Zahlen enthalten.")
        return value
    if kind is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("Task-JSON muss gültigen UTF-8-Text enthalten.") from exc
        return value
    if kind not in (dict, list):
        raise ValueError("Taskdaten müssen ausschließlich JSON-Werte enthalten.")
    ancestors = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in ancestors:
        raise ValueError("Task-JSON darf keine zyklischen Referenzen enthalten.")
    ancestors.add(identity)
    try:
        if kind is list:
            return [_json_snapshot(item, ancestors, depth + 1) for item in value]
        if any(type(key) is not str for key in value):
            raise ValueError("Task-JSON benötigt Textschlüssel.")
        return {_json_snapshot(key): _json_snapshot(item, ancestors, depth + 1)
                for key, item in value.items()}
    finally:
        ancestors.remove(identity)


def _utc(value, name, *, optional=False):
    if optional and value is None:
        return
    if (type(value) is not datetime or value.tzinfo is None
            or value.utcoffset() != timedelta(0)):
        raise ValueError(f"{name} muss ein UTC-Zeitstempel mit Zeitzone sein.")


def _validate(task):
    if type(task.schema_version) is not int or task.schema_version != 1:
        raise ValueError("Unbekannte Task-Schemaversion.")
    validate_task_id(task.task_id)
    _text(task.objective, "objective")
    _text(task.model_id, "model_id")
    if type(task.status) is not str or task.status not in TASK_STATUSES:
        raise ValueError("Unbekannter Task-Zustand.")
    if (type(task.max_attempts) is not int or task.max_attempts < 1
            or type(task.attempt_count) is not int
            or not 0 <= task.attempt_count <= task.max_attempts):
        raise ValueError("Ungültiger Task-Versuchszähler oder Maximalwert.")
    if type(task.constraints) is not dict:
        raise ValueError("constraints muss ein JSON-Objekt sein.")
    for name in ("result", "error"):
        value = getattr(task, name)
        if value is not None and type(value) is not dict:
            raise ValueError(f"{name} muss ein JSON-Objekt oder null sein.")
    for name in ("constraints", "result", "error"):
        _json_snapshot(getattr(task, name))
    for name in ("created_at", "updated_at"):
        _utc(getattr(task, name), name)
    for name in ("started_at", "completed_at"):
        _utc(getattr(task, name), name, optional=True)
    if task.updated_at < task.created_at:
        raise ValueError("updated_at darf nicht vor created_at liegen.")
    for name in ("started_at", "completed_at"):
        stamp = getattr(task, name)
        if stamp is not None and not task.created_at <= stamp <= task.updated_at:
            raise ValueError(f"{name} liegt außerhalb des Task-Zeitraums.")
    if (task.started_at is not None and task.completed_at is not None
            and task.completed_at < task.started_at):
        raise ValueError("Abschluss darf nicht vor dem Start liegen.")
    if task.status in {"queued", "cancelled"}:
        if task.attempt_count != 0 or task.started_at is not None or task.result is not None:
            raise ValueError("Nicht gestarteter Task darf keinen Versuch oder Ergebnis besitzen.")
    else:
        if task.attempt_count < 1 or task.started_at is None:
            raise ValueError("Gestarteter Task benötigt Versuchszähler und Startzeit.")
    if task.status in TERMINAL_STATUSES:
        if task.completed_at is None:
            raise ValueError("Terminaler Task benötigt eine Abschlusszeit.")
    elif task.completed_at is not None or task.result is not None or task.error is not None:
        raise ValueError("Nichtterminaler Task darf keine Abschlussdaten besitzen.")
    if task.status == "completed" and (task.result is None or task.error is not None):
        raise ValueError("Erfolgreicher Abschluss benötigt Ergebnis ohne Fehler.")
    if task.status == "failed" and task.error is None:
        raise ValueError("Fehlgeschlagener Abschluss benötigt einen Fehler.")


@dataclass(frozen=True)
class Task:
    schema_version: int
    task_id: str
    objective: str
    constraints: dict
    model_id: str
    status: str
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: dict | None
    error: dict | None

    def __post_init__(self):
        _validate(self)
        for name in ("constraints", "result", "error"):
            object.__setattr__(self, name, _json_snapshot(getattr(self, name)))

    @classmethod
    def create(cls, task_id, objective, constraints, model_id, max_attempts=1, *, now=None):
        now = datetime.now(timezone.utc) if now is None else now
        return cls(1, task_id, objective, constraints, model_id, "queued", 0,
                   max_attempts, now, now, None, None, None, None)

    def to_dict(self):
        """An independently mutable JSON snapshot, never a reference to inputs."""
        _validate(self)
        data = {name: getattr(self, name) for name in TASK_FIELDS}
        for name in ("created_at", "updated_at", "started_at", "completed_at"):
            stamp = data[name]
            data[name] = stamp.isoformat() if stamp is not None else None
        return _json_snapshot(data)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or set(data) != TASK_FIELDS:
            raise ValueError("Task-Schema benötigt genau die V1-Felder.")
        fields = dict(data)
        for name in ("created_at", "updated_at", "started_at", "completed_at"):
            stamp = fields[name]
            if stamp is None and name in {"started_at", "completed_at"}:
                continue
            if type(stamp) is not str:
                raise ValueError(f"{name} muss ISO-8601-Text sein.")
            try:
                fields[name] = datetime.fromisoformat(stamp)
            except ValueError as exc:
                raise ValueError(f"Ungültiger ISO-8601-Zeitstempel: {name}.") from exc
        return cls(**fields)
