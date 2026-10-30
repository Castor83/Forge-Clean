"""Read historical project files without changing or authorizing an order.

The files are separate observations, not a consistent or current approval.
This reader never opens a TaskStore session or verifies generated Python.
"""

import hashlib
import os
from pathlib import Path

from .conversation_json import load_conversation_json
from .conversation_protocol import parse_proposal_task
from .conversation_store import validate_task
from .project_context import (
    CONTEXT_FILENAME, MAX_CONTEXT_BYTES, _reject_links,
    fresh_project_context, validate_project_context,
)
from .project_session import (
    MARKER_FILENAME, REVOCATION_FILENAME, context_binding,
)


WORKSPACES_ROOT = Path(__file__).resolve().parents[2] / "workspaces"


def _workspace(directory):
    directory = Path(directory).absolute()
    root = Path(WORKSPACES_ROOT).absolute()
    # Reject foreign paths before querying their filesystem metadata.
    lexical_directory = Path(os.path.abspath(directory))
    lexical_root = Path(os.path.abspath(root))
    try:
        lexical_directory.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError("Inspektion ist nur in einem Unterordner von workspaces erlaubt.") from exc
    if lexical_directory == lexical_root:
        raise ValueError("Inspektion benötigt einen vorhandenen Auftragsunterordner.")
    _reject_links(directory)
    _reject_links(root)
    directory = directory.resolve(strict=True)
    root = root.resolve(strict=True)
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise ValueError("Inspektion ist nur in einem Unterordner von workspaces erlaubt.") from exc
    if directory == root or not directory.is_dir():
        raise ValueError("Inspektion benötigt einen vorhandenen Auftragsunterordner.")
    return directory


def _observation(name):
    return {"name": name, "status": "missing", "size": None, "sha256": None,
            "changed_during_read": False, "data": None}


def _signature(stat):
    # Windows stat/fstat may expose different ctime values for the same file.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _read(directory, name, maximum, problems):
    observation = _observation(name)
    path = directory / name
    try:
        _reject_links(path)
    except (OSError, ValueError) as exc:
        observation["status"] = "blocked"
        problems.append(name + ": " + str(exc))
        return observation, None
    try:
        if not path.exists():
            return observation, None
        if not path.is_file():
            raise ValueError("Keine reguläre Datei.")
        before = path.stat()
        observation["size"] = before.st_size
        if before.st_size > maximum:
            raise ValueError("Datei überschreitet das Inspektionslimit.")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
            after_read = os.fstat(stream.fileno())
        try:
            after = path.stat()
            changed = len({_signature(stat) for stat in (before, opened, after_read, after)}) != 1
        except OSError:
            changed = True
        observation["changed_during_read"] = changed
        if changed:
            problems.append(name + ": Datei änderte sich während des Lesens; keine gemeinsame Momentaufnahme.")
        if len(raw) > maximum:
            raise ValueError("Datei überschreitet das Inspektionslimit.")
        observation["sha256"] = hashlib.sha256(raw).hexdigest()
        observation["status"] = "changed" if changed else "read"
        return observation, raw
    except (OSError, ValueError) as exc:
        observation["status"] = "invalid"
        problems.append(name + ": " + str(exc))
        return observation, None


def _json_observation(directory, name, maximum, validate, problems):
    observation, raw = _read(directory, name, maximum, problems)
    if raw is not None:
        try:
            data = load_conversation_json(raw.decode("utf-8", errors="strict"))
            validate(data)
            observation["data"] = data
        except (OSError, ValueError) as exc:
            observation["status"] = "invalid"
            problems.append(name + ": " + str(exc))
    return observation


def _validate_marker(data):
    if (type(data) is not dict or set(data) != {"version", "project_id", "state"}
            or type(data["version"]) is not int or data["version"] != 1
            or type(data["state"]) is not str or data["state"] not in {"pending", "bound"}):
        raise ValueError("Ungültiger Bindungsmarker.")
    fresh_project_context(data["project_id"])


def _validate_revocation(data):
    if (type(data) is not dict or set(data) != {"version", "project_id"}
            or type(data["version"]) is not int or data["version"] != 1):
        raise ValueError("Ungültiger Widerruf.")
    fresh_project_context(data["project_id"])


def _validate_task(data):
    validate_task(data)
    if data["proposal"] is not None:
        parse_proposal_task(data["proposal"])


def inspect_project_store(directory: Path) -> dict:
    """Inspect one existing workspace, including an active or retained lock.

    Each file is read separately with a byte limit and link checks. Recorded
    hashes describe the bytes read, not a new host acceptance. A detected
    file change is reported; unchanged observations still do not establish
    a simultaneous snapshot or rule out changes between separate reads.
    """
    directory = _workspace(directory)
    problems = []
    observations = {
        "conversation": _json_observation(directory, "conversation.json", 1_000_000,
                                            _validate_task, problems),
        "project_context": _json_observation(directory, CONTEXT_FILENAME, MAX_CONTEXT_BYTES,
                                              validate_project_context, problems),
        "binding_marker": _json_observation(directory, MARKER_FILENAME, 1024,
                                             _validate_marker, problems),
        "revocation": _json_observation(directory, REVOCATION_FILENAME, 1024,
                                        _validate_revocation, problems),
    }
    lock, raw = _read(directory, "conversation.lock", 64, problems)
    if raw is not None:
        try:
            owner = raw.decode("ascii").strip()
            if not owner.isdecimal() or not 0 < int(owner) <= 4_294_967_295:
                raise ValueError("Prozessnummer der Sperre ist nicht lesbar.")
            lock["data"] = {"pid": int(owner)}
        except (UnicodeError, ValueError) as exc:
            lock["status"] = "invalid"
            problems.append("conversation.lock: " + str(exc))
    observations["lock"] = lock
    observations["build_file"] = _observation(None)

    task = observations["conversation"]["data"]
    context = observations["project_context"]["data"]
    marker = observations["binding_marker"]["data"]
    if task is None:
        if observations["conversation"]["status"] == "missing":
            problems.append("conversation.json: Gespeicherter Auftrag fehlt.")
    else:
        if task["proposal"] is not None:
            spec = parse_proposal_task(task["proposal"])
            name = "build/" + spec.task.allowed_files[0]
            file, source = _read(directory, name, 80_000, problems)
            if source is not None:
                try:
                    file["data"] = source.decode("utf-8", errors="strict")
                except UnicodeError as exc:
                    file["status"] = "invalid"
                    problems.append(name + ": " + str(exc))
            observations["build_file"] = file
        if task["version"] == 2:
            binding = task["context_binding"]
            if context is None:
                problems.append("Pflichtkontext des gespeicherten Projektauftrags ist nicht lesbar.")
            elif context_binding(context) != binding:
                problems.append("Gelesener Kontext stimmt nicht mit der gespeicherten Auftragsbindung überein.")
            if marker is None or marker["project_id"] != binding["project_id"]:
                problems.append("Gelesener Bindungsmarker passt nicht zum gespeicherten Auftrag.")
        elif (observations["project_context"]["status"] != "missing"
              or observations["binding_marker"]["status"] != "missing"):
            problems.append("Projektdateien bei Auftragsversion 1: keine gültige Projektbindung festgestellt.")
    if marker is not None and marker["state"] == "pending":
        problems.append("Bindungsmarker meldet einen ungeklärten Speichervorgang.")
    if observations["revocation"]["status"] != "missing":
        problems.append("Ein gespeicherter Widerruf ist vorhanden; der Auftrag bleibt gesperrt.")

    return {"inspection_only": True, "current_approval": False,
            "snapshot_consistency": "not_established", "workspace": str(directory),
            "problems": problems, "observations": observations}
