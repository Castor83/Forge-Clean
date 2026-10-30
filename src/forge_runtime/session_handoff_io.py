"""Bounded file access for the caller-selected local handoff workspace.

No discovery or execution occurs here. Path checks precede file metadata/read
operations; signatures catch observable changes, but do not eliminate TOCTOU.
"""

import hashlib
import os
import stat
import unicodedata
from pathlib import Path

from .project_context import _reject_links
from .workspace_tools import _has_ambiguous_windows_spelling


MAX_SOURCE_BYTES = 100_000
MAX_SOURCE_TOTAL_BYTES = 500_000
MAX_PACKAGE_BYTES = 250_000
MAX_SOURCES = 64
MAX_PATH_TEXT = 2_000


def canonical_relative(value):
    if type(value) is not str or not value or len(value) > MAX_PATH_TEXT:
        raise ValueError("Quellen-/Dateipfad muss ein begrenzter relativer Text sein.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Dateipfad ist kein gültiger UTF-8-Text.") from exc
    parts = value.split("/")
    if (len(encoded) > MAX_PACKAGE_BYTES or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in parts)
            or any(unicodedata.category(char) == "Cc" for char in value)
            or _has_ambiguous_windows_spelling(Path(value))):
        raise ValueError("Dateipfad ist nicht kanonisch oder verlässt die Arbeitswurzel.")
    for part in parts:
        device = part.split(".", 1)[0].rstrip(" ").upper()
        if (device in {"CONIN$", "CONOUT$", "CLOCK$"}
                or device in {prefix + number for prefix in ("COM", "LPT")
                              for number in "¹²³"}):
            raise ValueError("Windows-Gerätenamen sind als Dateipfad nicht zulässig.")
    return value


def source_paths(values):
    if type(values) not in (list, tuple) or not 1 <= len(values) <= MAX_SOURCES:
        raise ValueError("Quellenliste muss 1 bis 64 ausgewählte Pfade enthalten.")
    paths = [canonical_relative(value) for value in values]
    if len({value.casefold() for value in paths}) != len(paths):
        raise ValueError("Mehrfacher oder gleichartig geschriebener Quellenpfad.")
    return paths


def root_spelling(value):
    """Check root spelling without querying that root's filesystem."""
    if type(value) is not str or not value or len(value) > MAX_PATH_TEXT:
        raise ValueError("Ungültiger Arbeitswurzelpfad.")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Arbeitswurzel ist kein gültiger UTF-8-Text.") from exc
    # UNC/network and Windows device namespaces are not local workspace roots.
    if value.startswith(("\\\\", "//")):
        raise ValueError("Netzwerk-/UNC- und Gerätewurzeln sind nicht zulässig.")
    path = Path(value)
    if (not path.is_absolute() or any(unicodedata.category(char) == "Cc" for char in value)
            or _has_ambiguous_windows_spelling(Path(*path.parts[1:]))
            or any(part in {".", ".."} for part in path.parts)):
        raise ValueError("Arbeitswurzel muss ein eindeutiger absoluter Pfad sein.")
    return path


def checked_root(root):
    if not isinstance(root, (str, Path)):
        raise ValueError("Arbeitswurzel muss ein Pfad sein.")
    path = root_spelling(str(root))
    _reject_links(path)
    if not path.is_dir():
        raise ValueError("Arbeitswurzel fehlt oder ist kein Ordner.")
    resolved = path.resolve(strict=True)
    _reject_links(resolved)
    return resolved


def selected_path(root, relative):
    relative = canonical_relative(relative)
    path = root / Path(relative)
    # This lexical check deliberately happens before any foreign metadata.
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Dateipfad verlässt die Arbeitswurzel.") from exc
    _reject_links(path)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Dateipfad verlässt die Arbeitswurzel.") from exc
    if resolved != path:
        raise ValueError("Dateipfad besitzt einen nicht kanonischen Alias.")
    return path


def signature(info):
    # Windows path stat and handle fstat can expose different ctime values for
    # the same file (birth/change time API differences). Compare common fields.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns)


def read_regular(path, limit):
    """Read at most limit+1 bytes, rejecting observable path/handle changes."""
    _reject_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Ausgewählte Datei ist keine reguläre Datei.")
    if before.st_size > limit:
        raise ValueError("Ausgewählte Datei überschreitet das Byte-Limit.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if signature(before) != signature(opened) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("Datei wurde vor dem Öffnen verändert.")
        raw = stream.read(limit + 1)
        after_handle = os.fstat(stream.fileno())
    _reject_links(path)
    after_path = path.lstat()
    if (len(raw) > limit or len(raw) != before.st_size
            or signature(before) != signature(after_handle)
            or signature(before) != signature(after_path)):
        raise ValueError("Datei wurde während des begrenzten Lesens verändert.")
    return raw, signature(after_path)


def capture_sources(root, paths):
    records = []
    signatures = []
    total = 0
    for relative in paths:
        raw, token = read_regular(selected_path(root, relative),
                                  min(MAX_SOURCE_BYTES, MAX_SOURCE_TOTAL_BYTES - total))
        total += len(raw)
        records.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                        "size": len(raw)})
        signatures.append(token)
    # Re-read the complete selected set so a change to an earlier source while
    # reading a later one cannot silently produce a valid snapshot.
    problems, fresh_tokens = compare_sources(root, records)
    if problems or signatures != fresh_tokens:
        raise ValueError("Quellen änderten sich während der Erfassung: "
                         + "; ".join(problems or ["Dateisignatur geändert"]))
    return records


def compare_sources(root, records):
    problems = []
    tokens = []
    total = 0
    for record in records:
        try:
            raw, token = read_regular(selected_path(root, record["path"]),
                                      min(MAX_SOURCE_BYTES, MAX_SOURCE_TOTAL_BYTES - total))
            total += len(raw)
            if len(raw) != record["size"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
                problems.append("Quelle geändert: " + record["path"])
            tokens.append(token)
        except (OSError, ValueError) as exc:
            problems.append("Quelle nicht aktuell lesbar: " + record["path"] + ": " + str(exc))
            tokens.append(None)
    return problems, tokens


def fresh_sources(root, records):
    problems, before = compare_sources(root, records)
    second_problems, after = compare_sources(root, records)
    problems.extend(problem for problem in second_problems if problem not in problems)
    if before != after:
        problems.append("Quellen änderten sich zwischen den Kontrolllesungen.")
    return problems, after
