"""Explicit, bounded read-only Git observation. No handoff execution or writes."""

import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .session_handoff_io import checked_root, read_regular, root_spelling
from .project_context import _reject_links

MAX_OUTPUT_BYTES = 100_000
OBSERVATION_SECONDS = 30


def _run_bounded(command, root, env, deadline):
    if time.monotonic() >= deadline:
        raise ValueError("Git-Beobachtung überschreitet die Frist.")
    process = subprocess.Popen(
        command, cwd=root, env=env, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    buffers = [bytearray(), bytearray()]
    lock = threading.Lock()
    failed = threading.Event()
    errors = []

    def read_pipe(stream, target):
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                with lock:
                    if sum(map(len, buffers)) + len(chunk) > MAX_OUTPUT_BYTES:
                        failed.set()
                        break
                    buffers[target].extend(chunk)
        except OSError:
            errors.append(True)
            failed.set()
        finally:
            stream.close()

    readers = [threading.Thread(target=read_pipe, args=(stream, index), daemon=True)
               for index, stream in enumerate((process.stdout, process.stderr))]
    for reader in readers:
        reader.start()
    try:
        while process.poll() is None or any(reader.is_alive() for reader in readers):
            if failed.is_set():
                raise ValueError("Hilfsprozess-Ausgabe überschreitet das Limit oder Lesefehler.")
            if time.monotonic() >= deadline:
                raise ValueError("Git-Beobachtung überschreitet die Frist.")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if failed.is_set() or errors:
            raise ValueError("Ungültige Hilfsprozess-Ausgabe.")
        return process.returncode, bytes(buffers[0]), bytes(buffers[1])
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=0.1)


def _environment():
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_ATTR_NOSYSTEM="1")
    return env


def _check_metadata(root, deadline):
    metadata = root / ".git"
    _reject_links(metadata)
    if not metadata.is_dir():
        raise ValueError("Nur eigenständige Repositories mit .git-Ordner sind unterstützt.")
    count = 0
    pending = [metadata]
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                count += 1
                if count > 10_000 or time.monotonic() >= deadline:
                    raise ValueError("Git-Metadatenprüfung überschreitet ihr Budget.")
                _reject_links(Path(child.path))
                if child.is_dir(follow_symlinks=False):
                    pending.append(Path(child.path))
    for relative in ("commondir", "objects/info/alternates", "objects/info/http-alternates"):
        if (metadata / relative).exists():
            raise ValueError("Git-Metadatenumleitungen sind nicht unterstützt.")
    raw, _ = read_regular(metadata / "config", MAX_OUTPUT_BYTES)
    config = raw.decode("utf-8", errors="strict")
    if re.search(r"(?im)^\s*\[\s*(?:include|includeif|extensions|filter)\b", config):
        raise ValueError("Git-Konfigurationsincludes, Filter und Erweiterungen sind nicht unterstützt.")
    return raw


def _parse_status(raw):
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ValueError("Git-Status überschreitet das Ausgabelimit.")
    text = raw.decode("utf-8", errors="strict")
    if not text:
        return []
    if not text.endswith("\0"):
        raise ValueError("Git-Status fehlt die NUL-Begrenzung.")
    fields = text[:-1].split("\0")
    entries = []
    index = 0
    tracked = {left + right for left in " MTADRC" for right in " MDTRC"} - {"  "}
    valid = tracked | {"??", "DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2] != " ":
            raise ValueError("Ungültiger Git-Status-Eintrag.")
        status, path = field[:2], field[3:]
        if not path or status not in valid:
            raise ValueError("Ungültiger Git-Status oder Dateiname.")
        original = None
        if "R" in status or "C" in status:
            if index == len(fields) or not fields[index]:
                raise ValueError("Git-Rename-Ursprung fehlt.")
            original = fields[index]
            index += 1
        entries.append({"status": status, "path": path, "original_path": original})
    return entries


def _git_program(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("Host muss ein vorhandenes absolutes Gitprogramm wählen.")
    return str(path)


def observe_git(root, *, git_executable):
    """Return two matching observations; failures never become fresh results."""
    root = checked_root(root)
    executable = _git_program(git_executable)
    env = _environment()
    deadline = time.monotonic() + OBSERVATION_SECONDS
    config = _check_metadata(root, deadline)
    prefix = [executable, "--no-optional-locks",
              "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
              "-c", "core.quotePath=false", "-c", "diff.external=",
              "-c", "submodule.recurse=false", "-c", "maintenance.auto=false",
              "-c", "gc.auto=0"]
    prefix += ["-c", "core.worktree=" + str(root), "-c", "core.bare=false",
               "-c", "protocol.allow=never", "-c", "core.attributesFile=" + os.devnull,
               "-c", "core.excludesFile=" + os.devnull,
               "-c", "core.hooksPath=" + os.devnull]

    def command(args, allowed=(0,)):
        code, output, error = _run_bounded(prefix + args, root, env, deadline)
        # Validate both streams, including on Git failure. Do not echo untrusted stderr.
        output.decode("utf-8", errors="strict")
        error.decode("utf-8", errors="strict")
        if code not in allowed:
            raise ValueError("Git-Beobachtung fehlgeschlagen (Exitcode %s)." % code)
        return code, output

    def line(args):
        _, output = command(args)
        if not output.endswith(b"\n"):
            raise ValueError("Git-Einzelwert ohne Zeilenabschluss.")
        return output[:-1].decode("utf-8", errors="strict")

    def snapshot():
        observed_root = line(["rev-parse", "--show-toplevel"])
        if checked_root(observed_root) != root:
            raise ValueError("Gitwurzel stimmt nicht mit der Host-Arbeitswurzel überein.")
        head = line(["rev-parse", "--verify", "HEAD"])
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
            raise ValueError("Nicht unterstützter Git-Commitwert.")
        code, branch_raw = command(["symbolic-ref", "--quiet", "HEAD"], (0, 1))
        branch = None
        if code == 0:
            if not branch_raw.startswith(b"refs/heads/") or not branch_raw.endswith(b"\n"):
                raise ValueError("Ungültiger Git-Branchwert.")
            branch = branch_raw[len(b"refs/heads/"):-1].decode("utf-8", errors="strict")
            if not branch or any(ord(char) < 32 for char in branch):
                raise ValueError("Ungültiger Git-Branchwert.")
        elif branch_raw:
            raise ValueError("Detached HEAD enthält unerwarteten Branchwert.")
        _, status = command(["status", "--porcelain=v1", "-z",
                             "--untracked-files=all", "--ignore-submodules=all"])
        return {"version": 1, "repository_root": str(root), "branch": branch,
                "head": head, "entries": _parse_status(status)}

    first = snapshot()
    if first != snapshot():
        raise ValueError("Gitstand änderte sich zwischen den Beobachtungen.")
    if config != _check_metadata(root, deadline):
        raise ValueError("Gitkonfiguration änderte sich während der Beobachtung.")
    if time.monotonic() >= deadline:
        raise ValueError("Git-Beobachtung überschreitet die Frist.")
    return first


def git_is_fresh(root, observation, *, git_executable):
    """Equality proves only the recorded Git fields, never file content identity."""
    if (type(observation) is not dict or set(observation) !=
            {"version", "repository_root", "branch", "head", "entries"}
            or type(observation["version"]) is not int or observation["version"] != 1
            or type(observation["repository_root"]) is not str
            or (observation["branch"] is not None and type(observation["branch"]) is not str)
            or type(observation["head"]) is not str
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", observation["head"])
            or type(observation["entries"]) is not list
            or len(observation["entries"]) > MAX_OUTPUT_BYTES // 5):
        raise ValueError("Ungültige Git-Beobachtung.")
    root_spelling(observation["repository_root"])
    branch = observation["branch"]
    if branch is not None and (not branch or len(branch) > 2000
            or any(ord(char) < 33 or ord(char) == 127 or char in "~^:?*[\\" for char in branch)
            or ".." in branch or "@{" in branch
            or any(not part or part.startswith(".") or part.endswith((".", ".lock"))
                   for part in branch.split("/"))):
        raise ValueError("Ungültiger Git-Branchwert.")
    total = 0
    for entry in observation["entries"]:
        if (type(entry) is not dict or set(entry) != {"status", "path", "original_path"}
                or type(entry["status"]) is not str or type(entry["path"]) is not str
                or (entry["original_path"] is not None and type(entry["original_path"]) is not str)):
            raise ValueError("Ungültiger Git-Status-Eintrag.")
        if any(len(value) > MAX_OUTPUT_BYTES for value in entry.values() if value is not None):
            raise ValueError("Git-Status überschreitet das Ausgabelimit.")
        raw = entry["status"] + " " + entry["path"] + "\0"
        if entry["original_path"] is not None:
            raw += entry["original_path"] + "\0"
        encoded = raw.encode("utf-8")
        total += len(encoded)
        if total > MAX_OUTPUT_BYTES:
            raise ValueError("Git-Status überschreitet das Ausgabelimit.")
        if _parse_status(encoded) != [entry]:
            raise ValueError("Ungültiger Git-Status-Eintrag.")
    return observe_git(root, git_executable=git_executable) == observation
