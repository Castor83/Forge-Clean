"""Atomic local Task V1 storage, controlled by one exclusive PID session.

Path/handle checks reject observable aliases and file changes. They do not
provide OS isolation or eliminate malicious same-process filesystem races.
An uncertain post-replace failure is reported; the supervisor owns the policy
of retaining the PID lock and stopping further execution.
"""

import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .task_contract import Task, validate_task_id


MAX_TASK_BYTES = 1_000_000
_TRANSITIONS = {"queued": {"running", "cancelled"},
                "running": {"completed", "failed"},
                "completed": set(), "failed": set(), "cancelled": set()}
_IMMUTABLE = {"schema_version", "task_id", "objective", "constraints", "model_id",
              "max_attempts", "created_at"}


def _reject_links(path):
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("Lifecycle-Pfad darf keine Links oder Reparse-Punkte enthalten.")


def _signature(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Lifecycle-Datei muss eine eigene reguläre Datei ohne Links sein.")


def _read_bytes(path, limit):
    _reject_links(path)
    before = path.lstat()
    _regular(before)
    if before.st_size > limit:
        raise ValueError("Lifecycle-Datei überschreitet das Byte-Limit.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        if _signature(before) != _signature(opened):
            raise ValueError("Lifecycle-Datei wurde vor dem Öffnen verändert.")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    _reject_links(path)
    final = path.lstat()
    _regular(final)
    if (len(raw) > limit or len(raw) != before.st_size
            or _signature(before) != _signature(after)
            or _signature(before) != _signature(final)):
        raise ValueError("Lifecycle-Datei wurde während des Lesens verändert.")
    return raw


def _object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Doppelter Schlüssel in Lifecycle-JSON.")
        data[key] = value
    return data


def _invalid_constant(value):
    raise ValueError("Nichtendliche JSON-Zahl: " + value)


class LifecycleStore:
    def __init__(self, directory: Path):
        path = Path(directory).absolute()
        if (any(part == ".." for part in path.parts)
                or str(path).startswith(("\\\\", "//"))):
            raise ValueError("Lifecycle-Verzeichnis muss ein eindeutiger lokaler Pfad sein.")
        _reject_links(path)
        if not path.is_dir():
            raise ValueError("Lifecycle-Verzeichnis muss bereits bestehen.")
        self.directory = path.resolve(strict=True)
        _reject_links(self.directory)
        info = self.directory.stat()
        self._directory_identity = (info.st_dev, info.st_ino)
        self.lock_path = self.directory / ".lifecycle.lock"
        self.retain_lock_on_close = False
        self._locked = False
        self._lock_identity = None
        self._session_pid = None
        self._mutex = threading.RLock()

    def _check_directory(self):
        _reject_links(self.directory)
        info = self.directory.stat()
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != self._directory_identity):
            raise ValueError("Lifecycle-Verzeichnis wurde ersetzt.")

    def _require_session(self):
        if not self._locked or self._session_pid != os.getpid():
            raise RuntimeError("LifecycleStore benötigt eine exklusive Sitzung.")
        self._check_directory()
        _reject_links(self.lock_path)
        info = self.lock_path.lstat()
        _regular(info)
        if (info.st_dev, info.st_ino) != self._lock_identity:
            raise RuntimeError("Lifecycle-Sitzungssperre wurde ersetzt.")

    @contextmanager
    def exclusive(self):
        with self._mutex:
            if self._locked:
                raise RuntimeError("LifecycleStore ist bereits exklusiv geöffnet.")
            self._check_directory()
            _reject_links(self.lock_path)
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.lock_path, flags, 0o600)
            except FileExistsError as exc:
                raise RuntimeError(f"Lifecycle-Sitzung ist gesperrt: {self.lock_path}; "
                                   "PID und gespeicherten Stand vor manueller Entfernung prüfen.") from exc
            try:
                with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
                    info = os.fstat(stream.fileno())
                    _regular(info)
                    self._lock_identity = info.st_dev, info.st_ino
                    self._session_pid = os.getpid()
                    stream.write(str(self._session_pid) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._locked = True
            except BaseException:
                # An incomplete ownership record is intentionally not auto-deleted.
                self.retain_lock_on_close = True
                raise
        try:
            yield self
        finally:
            with self._mutex:
                try:
                    if not self.retain_lock_on_close:
                        self._require_session()
                        self.lock_path.unlink()
                finally:
                    self._locked = False
                    self._lock_identity = None
                    self._session_pid = None

    def _path(self, task_id):
        validate_task_id(task_id)
        path = self.directory / (task_id + ".json")
        _reject_links(path)
        return path

    def _load(self, task_id):
        path = self._path(task_id)
        try:
            data = json.loads(_read_bytes(path, MAX_TASK_BYTES).decode("utf-8"),
                              object_pairs_hook=_object, parse_constant=_invalid_constant)
        except (UnicodeError, RecursionError) as exc:
            raise ValueError("Lifecycle-Datei enthält kein begrenztes UTF-8-JSON.") from exc
        task = Task.from_dict(data)
        if task.task_id != task_id:
            raise ValueError("Lifecycle-Dateiname und Task-ID stimmen nicht überein.")
        return task

    def load(self, task_id):
        with self._mutex:
            self._require_session()
            return self._load(task_id)

    def list_tasks(self):
        with self._mutex:
            self._require_session()
            names = sorted(path.name for path in self.directory.iterdir()
                           if path.name.lower().endswith(".json"))
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("Lifecycle-Verzeichnis enthält mehrdeutige Dateinamen.")
            return tuple(self._load(name[:-5]) for name in names)

    def _write(self, task):
        path = self._path(task.task_id)
        content = (json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True,
                              indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(content) > MAX_TASK_BYTES:
            raise ValueError("Task-Snapshot überschreitet das Byte-Limit.")
        fd, name = tempfile.mkstemp(prefix=".lifecycle-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._require_session()
            _reject_links(path)
            if path.exists():
                _regular(path.lstat())
            os.replace(name, path)
            actual = _read_bytes(path, MAX_TASK_BYTES)
            if actual != content:
                raise OSError("Lifecycle-Schreibprüfung stimmt nicht mit dem Snapshot überein.")
            restored = self._load(task.task_id)
            if restored.to_dict() != task.to_dict():
                raise OSError("Lifecycle-Schreibprüfung hat einen abweichenden Task ergeben.")
        finally:
            if os.path.lexists(name):
                _reject_links(Path(name))
                os.unlink(name)

    def create(self, task: Task):
        with self._mutex:
            self._require_session()
            if type(task) is not Task:
                raise ValueError("LifecycleStore akzeptiert ausschließlich Task-Snapshots.")
            task = Task.from_dict(task.to_dict())
            if (task.status != "queued" or task.attempt_count != 0
                    or task.updated_at != task.created_at):
                raise ValueError("create benötigt einen neuen unveränderten queued Task.")
            path = self._path(task.task_id)
            if any(candidate.name.casefold() == path.name.casefold()
                   for candidate in self.directory.iterdir()):
                raise FileExistsError("Task-ID ist bereits gespeichert: " + task.task_id)
            self._write(task)

    def save(self, task: Task):
        with self._mutex:
            self._require_session()
            if type(task) is not Task:
                raise ValueError("LifecycleStore akzeptiert ausschließlich Task-Snapshots.")
            task = Task.from_dict(task.to_dict())
            previous = self._load(task.task_id)
            old, new = previous.to_dict(), task.to_dict()
            if any(old[name] != new[name] for name in _IMMUTABLE):
                raise ValueError("Unveränderlicher Taskvertrag darf nicht ersetzt werden.")
            if task.updated_at < previous.updated_at:
                raise ValueError("Task-Aktualisierungszeit darf nicht rückwärts laufen.")
            if task.status == previous.status:
                if any(old[name] != new[name] for name in old if name != "updated_at"):
                    raise ValueError("Gleicher Zustand erlaubt nur unveränderte Snapshots.")
            else:
                if task.status not in _TRANSITIONS[previous.status]:
                    raise ValueError("Verbotener Task-Zustandsübergang.")
                event_at = task.started_at if task.status == "running" else task.completed_at
                if event_at < previous.updated_at:
                    raise ValueError("Task-Übergang darf nicht vor dem letzten Snapshot liegen.")
                increment = 1 if previous.status == "queued" and task.status == "running" else 0
                if task.attempt_count != previous.attempt_count + increment:
                    raise ValueError("Versuchszähler darf sich nur genau beim Laufstart erhöhen.")
                if previous.started_at is not None and task.started_at != previous.started_at:
                    raise ValueError("Task-Startzeit darf nach dem Start nicht ersetzt werden.")
            self._write(task)
