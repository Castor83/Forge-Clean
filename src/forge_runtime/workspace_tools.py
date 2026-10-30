import os
import tempfile
from pathlib import Path

from .contracts import SubagentTask


_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class WorkspaceFileTools:
    """Read and atomically write only task-approved files below one workspace."""

    def __init__(self, root: Path, task: SubagentTask):
        if not isinstance(root, Path):
            raise ValueError("root must be a pathlib.Path")
        if type(task) is not SubagentTask:
            raise ValueError("task must be a SubagentTask")

        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError("root must be an existing directory")

        writable = {}
        for relative_path in task.allowed_files:
            resolved = self._resolve_workspace_path(relative_path)
            writable[relative_path] = resolved
        read_only = {}
        for relative_path in task.read_only_files:
            resolved = self._resolve_workspace_path(relative_path)
            read_only[relative_path] = resolved
        self._writable = writable
        self._readable = {**writable, **read_only}

    def _resolve_workspace_path(self, relative_path: str) -> Path:
        if type(relative_path) is not str or not relative_path.strip():
            raise ValueError("relative_path must be a non-empty string")

        supplied = Path(relative_path)
        if supplied.is_absolute() or supplied.drive or ".." in supplied.parts:
            raise ValueError("path must stay within the workspace")
        if _has_ambiguous_windows_spelling(supplied):
            raise ValueError("path uses an unsupported Windows path spelling")

        resolved = (self.root / supplied).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path must stay within the workspace") from exc
        return resolved

    def _registered_path(
        self, relative_path: str, registered: dict[str, Path], error: str
    ) -> Path:
        resolved = self._resolve_workspace_path(relative_path)
        allowed = registered.get(relative_path)
        if allowed is None or resolved != allowed:
            raise ValueError(error)
        return resolved

    def read_text(self, relative_path: str) -> str:
        path = self._registered_path(
            relative_path,
            self._readable,
            "path is not allowed to be read for this task",
        )
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path.read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> None:
        if type(content) is not str:
            raise ValueError("content must be a string")
        path = self._registered_path(
            relative_path,
            self._writable,
            "path is not allowed to be written for this task",
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary_path, path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _has_ambiguous_windows_spelling(path: Path) -> bool:
    for part in path.parts:
        if (
            any(character in '<>:"|?*' for character in part)
            or any(ord(character) < 32 for character in part)
            or part.endswith((" ", "."))
        ):
            return True
        device_name = part.split(".", 1)[0].rstrip(" ").upper()
        if device_name in _WINDOWS_RESERVED_NAMES:
            return True
    return False
