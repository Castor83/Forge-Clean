"""Forge entry point selecting its registered local Ollama profile explicitly."""

import argparse
import json
import sys
from pathlib import Path

from .ollama_runtime import OllamaRuntime, PROFILE


def _validate_action_paths(args):
    """Retain existing workspace/task boundaries before any model contact."""
    if args.action not in {"chat", "run"}:
        return
    root = Path(__file__).resolve().parents[2]
    if args.action == "chat":
        from .conversation_cli import require_task_child
        from .workspace_tools import _has_ambiguous_windows_spelling
        base = root / "workspaces"
        directory = Path(args.workspace).absolute()
        require_task_child(directory, base)
        if _has_ambiguous_windows_spelling(directory.relative_to(base)):
            raise ValueError("Arbeitsordner verwendet eine ungueltige Windows-Pfadschreibweise.")
        for candidate in (directory, *directory.parents):
            if candidate == base.parent:
                break
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError("Arbeitsordner darf keine Links enthalten.")
            if candidate.exists() and not candidate.is_dir():
                raise ValueError("Arbeitsordner und seine Eltern muessen Verzeichnisse sein.")
        require_task_child(directory.resolve(strict=False), base.resolve(strict=False))
        return
    from .cli import _require_inside_repository
    from .task_runner import parse_task_spec
    from .workspace_tools import WorkspaceFileTools
    workspace = Path(args.workspace).resolve(strict=True)
    task_file = Path(args.task_file).resolve(strict=True)
    _require_inside_repository(workspace, root, "workspace")
    _require_inside_repository(task_file, root, "task file")
    if not workspace.is_dir() or not task_file.is_file():
        raise ValueError("Arbeitsordner oder Aufgaben-Datei ist ungueltig.")
    spec = parse_task_spec(task_file.read_text(encoding="utf-8"))
    WorkspaceFileTools(workspace, spec.task)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Forge mit dem vorhandenen lokalen Ollama-Modell starten.")
    parser.add_argument("--action", choices=("status", "start", "stop", "chat", "run"), default="status")
    parser.add_argument("--workspace")
    parser.add_argument("--task-file")
    args = parser.parse_args(argv)
    if args.action in {"chat", "run"} and not args.workspace:
        parser.error("--workspace fehlt")
    if args.action == "run" and not args.task_file:
        parser.error("--task-file fehlt")
    runtime = OllamaRuntime()
    try:
        _validate_action_paths(args)
        if args.action == "status":
            print(json.dumps(runtime.status(), ensure_ascii=True, indent=2))
            return 0
        if args.action == "stop":
            print(json.dumps(runtime.stop(), ensure_ascii=True))
            return 0
        ready = runtime.start()
        if args.action == "start":
            print(f"Forge-Modell in Ollama geladen: {PROFILE.model_id}, Kontext {ready['loaded_model']['context_length']}.")
            return 0
        options = ["--workspace", args.workspace, "--model", PROFILE.model_id, "--endpoint", PROFILE.endpoint]
        if args.action == "chat":
            from .conversation_cli import main as chat_main
            return chat_main(options, chat_factory=runtime.chat_factory)
        from .cli import main as build_main
        return build_main(options + ["--task-file", args.task_file], chat_factory=runtime.chat_factory)
    except (OSError, RuntimeError, ValueError) as exc:
        print("Forge mit Ollama konnte nicht starten: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
