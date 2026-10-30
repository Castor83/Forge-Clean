"""Host-created synthetic roots only; bounded snapshots, not OS isolation.

Observable links and file changes fail closed. Same-user filesystem races and
blocking OS reads are not fully prevented. No generated code receives paths.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
from types import MappingProxyType


REPOSITORY = Path(__file__).resolve().parents[2]
_HOST_SEAL = object()
FIXTURE_NAMES = ('empty', 'unicode', 'crlf', 'invalid_utf8', 'missing_root', 'mixed')
_RESERVED = (
    {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$'}
    | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}
    | {prefix + number for prefix in ('COM', 'LPT') for number in '¹²³'}
)


@dataclass(frozen=True, slots=True)
class F001InputBudget:
    entries: int = 64
    files: int = 32
    file_bytes: int = 65536
    total_bytes: int = 262144
    name_chars: int = 1024

    def __post_init__(self):
        values = (self.entries, self.files, self.file_bytes, self.total_bytes, self.name_chars)
        if any(type(value) is not int for value in values) or values != (64, 32, 65536, 262144, 1024):
            raise ValueError('F-001 preparation budgets are fixed')


class _InputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _valid_name(name):
    return (type(name) is str and 0 < len(name) <= 1024 and name not in ('.', '..')
            and not any(c in '<>:"|?*/\\' or ord(c) < 32 for c in name)
            and not name.endswith((' ', '.'))
            and name.split('.')[0].rstrip(' ').upper() not in _RESERVED)


def _signature(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_nlink


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _reject_links(path):
    try:
        path.relative_to(REPOSITORY)
    except ValueError as exc:
        raise _InputError('unsafe_path') from exc
    candidate = path
    while True:
        if _is_link(candidate.lstat()):
            raise _InputError('unsafe_path')
        if candidate == REPOSITORY:
            break
        candidate = candidate.parent


def _directory_names(path):
    names = []
    with os.scandir(path) as entries:
        for entry in entries:
            if len(names) == 64:
                raise _InputError('limit_exceeded')
            names.append(entry.name)
    return names


def _regular(info):
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _InputError('unsafe_path')


def _read_regular_bounded(path, before, limit):
    _reject_links(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        if _signature(opened) != _signature(before):
            raise _InputError('input_changed')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    try:
        _reject_links(path)
        final = path.lstat()
    except OSError as exc:
        raise _InputError('input_changed') from exc
    _regular(final)
    if (len(raw) > limit or len(raw) != before.st_size
            or _signature(before) != _signature(after) or _signature(before) != _signature(final)):
        raise _InputError('input_changed')
    return raw


class BoundF001Inputs:
    """A host-owned lifetime, never constructed from model-chosen roots."""

    def __init__(self, directory, *, _seal=None):
        if _seal is not _HOST_SEAL:
            raise ValueError('Use host-created synthetic F-001 fixtures')
        self.directory = directory
        self.budget = F001InputBudget()
        self._active = True
        self._parent_identity = self._identity(directory)
        self._roots = MappingProxyType({name: directory/name for name in FIXTURE_NAMES if name != 'missing_root'})
        self._identities = MappingProxyType({name: self._identity(path) for name, path in self._roots.items()})

    @staticmethod
    def _identity(path):
        _reject_links(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise _InputError('invalid_root')
        return info.st_dev, info.st_ino

    def _validate_root(self, name):
        if self._identity(self.directory) != self._parent_identity:
            raise _InputError('input_changed')
        path = self._roots[name]
        if self._identity(path) != self._identities[name]:
            raise _InputError('input_changed')
        return path

    def snapshot(self, root_id):
        if (not self._active or type(root_id) is not str or len(root_id) > 64
                or re.fullmatch(r'[A-Za-z0-9_-]+', root_id) is None):
            return {'root_error': 'invalid_root', 'files': []}
        if root_id not in self._roots:
            return {'root_error': 'unknown_root', 'files': []}
        try:
            path = self._validate_root(root_id)
            before_directory = _signature(path.lstat())
            names = _directory_names(path)
            if any(not _valid_name(name) for name in names) or len({name.casefold() for name in names}) != len(names):
                raise _InputError('unsafe_path')
            selected = []
            total = 0
            for name in sorted(names):
                if not name.casefold().endswith('.txt'):
                    continue
                info = (path/name).lstat()
                if stat.S_ISDIR(info.st_mode) and not _is_link(info):
                    continue
                if len(selected) == self.budget.files:
                    raise _InputError('limit_exceeded')
                error = None
                try:
                    _regular(info)
                except _InputError:
                    error = 'unsafe_path'
                if error is None:
                    if info.st_size > self.budget.file_bytes:
                        error = 'limit_exceeded'
                    else:
                        total += info.st_size
                selected.append((name, info, error))
            if total > self.budget.total_bytes:
                raise _InputError('limit_exceeded')
            records = []
            read_bytes = 0
            for name, info, error in selected:
                raw = None
                if error is None:
                    try:
                        raw = _read_regular_bounded(path/name, info, self.budget.file_bytes)
                        read_bytes += len(raw)
                        if read_bytes > self.budget.total_bytes:
                            raise _InputError('limit_exceeded')
                    except OSError:
                        error = 'read_error'
                records.append({'name': name, 'raw': raw, 'error': error})
            self._validate_root(root_id)
            if _signature(path.lstat()) != before_directory:
                raise _InputError('input_changed')
            return {'root_error': None, 'files': records}
        except _InputError as exc:
            return {'root_error': exc.code, 'files': []}
        except OSError:
            return {'root_error': 'read_error', 'files': []}


@contextmanager
def create_f001_inputs():
    """Create only fixed synthetic fixtures; callers cannot supply real roots."""
    with TemporaryDirectory(prefix='f001-', dir=REPOSITORY) as temporary:
        directory = Path(temporary)
        for name in FIXTURE_NAMES:
            if name != 'missing_root':
                (directory/name).mkdir()
        (directory/'unicode'/'ä.txt').write_bytes('äx\n'.encode('utf-8'))
        (directory/'crlf'/'a.txt').write_bytes(b'a\r\nb\r\n')
        (directory/'invalid_utf8'/'bad.txt').write_bytes(b'\xff')
        (directory/'mixed'/'bad.txt').write_bytes(b'\xff')
        (directory/'mixed'/'ok.txt').write_bytes(b'one\n')
        inputs = BoundF001Inputs(directory, _seal=_HOST_SEAL)
        try:
            yield inputs
        finally:
            inputs._active = False
