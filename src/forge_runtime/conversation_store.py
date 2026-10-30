"""Atomic local conversation state; one controlling process per workspace."""

import copy
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .contracts import PipelineResult
from .conversation_json import load_conversation_json
from .project_session import (_bound_context, guarded_project_chat, initialize_project, require_project_context,
                              record_context_failure, recover_project_store, validate_context_binding)
from .result_parser import parse_subagent_result


STATES = {"clarifying", "proposed", "ready", "running", "completed",
          "failed", "cancelled", "interrupted"}
FIELDS = {"version", "state", "messages", "questions", "proposal", "events", "report"}


def fresh_task():
    return {"version": 1, "state": "clarifying", "messages": [],
            "questions": [], "proposal": None, "events": [], "report": None,
            "confirmed_proposal_sha256": None}


def proposal_digest(proposal):
    encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_task(data, *, writing=False):
    context_fields = {"confirmed_proposal_sha256", "context_binding", "confirmed_context_sha256"}
    if type(data) is not dict or set(data) not in (FIELDS, FIELDS | {"confirmed_proposal_sha256"}, FIELDS | context_fields):
        raise ValueError("Ungültige Auftragsdatei: Felder stimmen nicht.")
    if type(data["version"]) is not int or data["version"] not in (1, 2):
        raise ValueError("Unbekannte Auftragsversion.")
    if data["version"] == 2:
        if set(data) != FIELDS | context_fields:
            raise ValueError("Projektauftrag benötigt die vollständige Kontextbindung.")
        validate_context_binding(data["context_binding"])
        confirmed_context = data["confirmed_context_sha256"]
        if confirmed_context is not None and (type(confirmed_context) is not str
                or len(confirmed_context) != 64
                or any(char not in "0123456789abcdef" for char in confirmed_context)):
            raise ValueError("Ungültige Kontextbestätigung.")
        if writing and data["state"] == "ready" and confirmed_context != data["context_binding"]["sha256"]:
            raise ValueError("Bereiter Auftrag benötigt die Bestätigung des Projektstands.")
    elif set(data) == FIELDS | context_fields:
        raise ValueError("Kontextfelder benötigen Auftragsversion 2.")
    if type(data["state"]) is not str or data["state"] not in STATES:
        raise ValueError("Unbekannter Auftragszustand.")
    digest = data.get("confirmed_proposal_sha256")
    if digest is not None and (type(digest) is not str or len(digest) != 64
                               or any(char not in "0123456789abcdef" for char in digest)):
        raise ValueError("Ungültige Bestätigungsbindung.")
    for key in ("questions", "events"):
        if type(data[key]) is not list or any(type(x) is not str or not x.strip() for x in data[key]):
            raise ValueError("Ungültige Fragen oder Ereignisse.")
    if type(data["messages"]) is not list:
        raise ValueError("Ungültiges Gespräch.")
    for message in data["messages"]:
        if (type(message) is not dict or set(message) != {"role", "content"}
                or message["role"] not in ("user", "assistant")
                or type(message["content"]) is not str or not message["content"].strip()):
            raise ValueError("Ungültige Gesprächsnachricht.")
    if data["proposal"] is not None and type(data["proposal"]) is not dict:
        raise ValueError("Ungültiger Bauvorschlag.")
    if data["report"] is not None and type(data["report"]) is not dict:
        raise ValueError("Ungültiger Prüfbericht.")
    report = data["report"]
    if report is not None:
        for field in ("approved", "host_passed", "pipeline_approved", "resume", "intent_cases_passed"):
            if field in report and type(report[field]) is not bool:
                raise ValueError("Ungültiges Freigabefeld im Prüfbericht.")
        if "results" in report:
            if type(report["results"]) is not list or not 1 <= len(report["results"]) <= 3:
                raise ValueError("Ungültiger Rollenbericht.")
            results = tuple(parse_subagent_result(role, json.dumps(item))
                            for role, item in zip(("builder", "tester", "reviewer"), report["results"]))
            PipelineResult(results)
    if data["state"] in {"proposed", "ready", "running", "completed", "failed", "interrupted"}:
        if data["proposal"] is None or data["questions"]:
            raise ValueError("Zustand benötigt einen geklärten Bauvorschlag.")
    if data["state"] == "completed":
        if (not report or report.get("approved") is not True
                or report.get("host_passed") is not True
                or report.get("intent_cases_passed") is False
                or type(report.get("results")) is not list or len(report["results"]) != 3):
            raise ValueError("Abschluss benötigt einen erfolgreichen Host- und Rollenbericht.")
        if not PipelineResult(results).approved:
            raise ValueError("Abschluss ohne erfolgreiche Rollenfreigabe ist ungültig.")
    if writing and data["state"] == "ready" and digest != proposal_digest(data["proposal"]):
        raise ValueError("Bereiter Auftrag benötigt die Bestätigung genau dieses Vorschlags.")


class TaskStore:
    def __init__(self, directory: Path):
        self.directory = directory.resolve(strict=True)
        if not self.directory.is_dir():
            raise ValueError("Arbeitsordner fehlt.")
        self.path = self.directory / "conversation.json"
        self.lock_path = self.directory / "conversation.lock"
        self._locked = False
        self.retain_lock_on_close = False
        self.project_uncertain = False

    def lock_message(self):
        owner = None
        try:
            if not (self.lock_path.is_symlink() or self.lock_path.is_junction()) and self.lock_path.is_file():
                with self.lock_path.open("rb") as stream:
                    raw = stream.read(65)
                if len(raw) <= 64:
                    value = raw.decode("ascii").strip()
                    if value.isdecimal() and 0 < int(value) <= 4_294_967_295:
                        owner = str(int(value))
        except (OSError, UnicodeError):
            pass
        location = f"Auftrag gesperrt: {self.lock_path}. "
        recovery = (" Gespeicherten Auftragsstand ebenfalls prüfen. Nach einem Speicherfehler "
                    "vor /bauen den vollständigen aktuellen Wunsch erneut eingeben und "
                    "den neuen Vorschlag bestätigen.")
        if owner is None:
            return location + "PID nicht lesbar; Prozesszuordnung und Sperrdatei vor manueller Entfernung prüfen." + recovery
        return (location + f"Notierte Prozessnummer (PID): {owner}. "
                "Im Task-Manager prüfen, ob dieser Prozess zum Forge-Auftrag gehört und noch läuft. "
                "Sperrdatei erst nach dieser Prüfung manuell entfernen." + recovery)

    @contextmanager
    def exclusive(self):
        if self._locked:
            raise RuntimeError("Auftrag ist bereits geöffnet.")
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(self.lock_message()) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            self._locked = True
            yield self
        finally:
            self._locked = False
            if not self.retain_lock_on_close:
                self.lock_path.unlink()

    def load(self):
        try:
            return self._load_data()
        except (OSError, ValueError):
            if self._locked:
                record_context_failure(self)
            raise

    def _load_data(self):
        if not self._locked:
            raise RuntimeError("Auftrag muss exklusiv geöffnet sein.")
        if self.path.is_symlink() or self.path.is_junction():
            raise ValueError("Auftragsdatei darf kein Link sein.")
        if not self.path.exists():
            return fresh_task()
        if self.path.stat().st_size > 1_000_000:
            raise ValueError("Auftragsdatei ist zu groß.")
        data = load_conversation_json(self.path.read_text(encoding="utf-8"))
        validate_task(data)
        return data

    def save(self, data):
        if not self._locked:
            raise RuntimeError("Auftrag muss exklusiv geöffnet sein.")
        validate_task(data, writing=True)
        if self.path.is_symlink() or self.path.is_junction():
            raise ValueError("Auftragsdatei darf kein Link sein.")
        content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("Gespräch ist zu groß; neuen Auftrag beginnen.")
        fd, name = tempfile.mkstemp(prefix="conversation-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self.retain_lock_on_close = self.project_uncertain
        finally:
            if os.path.exists(name):
                os.unlink(name)


class ConversationTask:
    def __init__(self, store: TaskStore):
        self.store = store
        self.data = store.load()
        # Reject inconsistent persisted context before rewriting any user file.
        try:
            _bound_context(store, self.data)
        except (OSError, ValueError):
            record_context_failure(store, self.data.get("context_binding", {}).get("project_id"))
            raise
        from .session_automation import validate_session
        validate_session(self)
        if self.data["state"] == "running":
            self.change(state="interrupted", event="Vorheriger Bau wurde unterbrochen; Dateien vor Wiederaufnahme prüfen.")
        elif self.data["state"] == "ready" and not self.is_confirmed():
            self.change(state="proposed", confirmed_proposal_sha256=None, report=None,
                        event="Gespeicherte Bestätigung passt nicht zum Vorschlag; erneut prüfen und bestätigen.")

    def is_confirmed(self):
        context = require_project_context(self)
        return (not self.store.retain_lock_on_close and self.data["state"] == "ready" and self.data.get("confirmed_proposal_sha256")
                == proposal_digest(self.data["proposal"]) and (context is None
                or self.data["confirmed_context_sha256"] == self.data["context_binding"]["sha256"]))

    def project_data(self):
        return require_project_context(self)

    def initialize_project(self, project_id):
        initialize_project(self, project_id)

    def guarded_chat(self, chat, *, build_proposal=None):
        guarded = guarded_project_chat(self, chat, build_proposal=build_proposal)

        def session_guard(messages):
            from .session_automation import validate_session
            validate_session(self)
            result = guarded(messages)
            validate_session(self)
            return result

        configure = getattr(guarded, "set_tool_actions", None)
        if configure is not None:
            session_guard.set_tool_actions = configure
        return session_guard

    def recover_project(self, project_id):
        self.data = recover_project_store(self.store, project_id)

    def change(self, *, state=None, event=None, **fields):
        try:
            candidate = copy.deepcopy(self.data)
            candidate.update(fields)
            if "proposal" in fields:
                candidate["confirmed_proposal_sha256"] = None
                if candidate["version"] == 2:
                    candidate["confirmed_context_sha256"] = None
            if state is not None:
                candidate["state"] = state
            if event is not None:
                candidate["events"].append(event)
            self.store.save(candidate)
        except (OSError, ValueError, KeyboardInterrupt):
            if self.data["state"] == "ready":
                # The disk state may still carry the old confirmation.
                # Revoke it in memory and leave the session lock until a write succeeds.
                self.store.retain_lock_on_close = True
                self.data = dict(self.data)
                self.data.update(state="clarifying", proposal=None, report=None,
                    confirmed_proposal_sha256=None,
                    questions=["Letzter Speicherstand ungeklärt; Fehler beheben und Wunsch erneut eingeben."])
                self.data["events"] = self.data["events"] + ["Speichern fehlgeschlagen; alte Baubestätigung gesperrt."]
            raise
        self.data = candidate

    def confirm(self):
        context = require_project_context(self)
        if self.data["state"] != "proposed":
            raise ValueError("Zuerst einen vollständigen Bauvorschlag klären.")
        fields = {} if context is None else {"confirmed_context_sha256": self.data["context_binding"]["sha256"]}
        self.change(state="ready", confirmed_proposal_sha256=proposal_digest(self.data["proposal"]), **fields,
                    event="Benutzer hat den angezeigten Bauvorschlag bestätigt.")

    def cancel(self):
        if self.store.project_uncertain:
            raise ValueError("Projekt-Speicherstand ungeklärt; Abbruch kann die Sperre nicht freigeben.")
        if self.data["state"] in {"completed", "cancelled"}:
            raise ValueError("Dieser Auftrag ist bereits beendet.")
        self.change(state="cancelled", event="Benutzer hat den Auftrag abgebrochen.")
