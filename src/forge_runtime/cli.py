import argparse
import json
import sys
from pathlib import Path

from .lm_studio_executor import LMStudioChatClient
from .task_runner import format_run_report, parse_task_spec, run_forge_task


_LOCAL_MODEL_TIMEOUT_SECONDS = 300.0
_ROLE_DEADLINE_SECONDS = 900.0
_MAX_TOOL_STEPS = 20


def main(argv: list[str] | None = None, *, chat_factory=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one restricted local Forge build task."
    )
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--model", default="forge-30b")
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:1234/v1/chat/completions",
    )
    args = parser.parse_args(argv)

    try:
        repository_root = Path(__file__).resolve().parents[2]
        task_file = Path(args.task_file).resolve(strict=True)
        workspace = Path(args.workspace).resolve(strict=True)
        _require_inside_repository(task_file, repository_root, "task file")
        _require_inside_repository(workspace, repository_root, "workspace")
        if not task_file.is_file():
            raise ValueError("task file must be a file")
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")

        spec = parse_task_spec(task_file.read_text(encoding="utf-8"))
        factory = LMStudioChatClient if chat_factory is None else chat_factory
        if not callable(factory):
            raise ValueError("chat_factory must be callable")
        chat = factory(
            model=args.model,
            endpoint=args.endpoint,
            timeout_seconds=_LOCAL_MODEL_TIMEOUT_SECONDS,
            response_kind="tool_action",
        )
        result = run_forge_task(
            workspace,
            spec,
            chat,
            max_steps=_MAX_TOOL_STEPS,
            max_call_seconds=_LOCAL_MODEL_TIMEOUT_SECONDS,
            max_elapsed_seconds=_ROLE_DEADLINE_SECONDS,
            max_repair_attempts=1,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        error = json.dumps(
            {"error": str(exc), "status": "error"},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        print(error, file=sys.stderr)
        return 1

    print(format_run_report(result), end="")
    return 0 if result.approved else 2


def _require_inside_repository(path: Path, repository_root: Path, label: str) -> None:
    try:
        path.relative_to(repository_root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the Forge repository") from exc


if __name__ == "__main__":
    raise SystemExit(main())
