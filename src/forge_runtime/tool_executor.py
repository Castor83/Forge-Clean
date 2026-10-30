import math
from pathlib import Path

from .contracts import SubagentResult, SubagentTask
from .host_policy import HostOnlyDecision, host_decision
from .tool_loop import run_tool_loop
from .workspace_tools import WorkspaceFileTools


class ForgeToolExecutor:
    """Bind a chat model to Forge's role pipeline and restricted workspace."""

    def __init__(
        self,
        root: Path,
        chat,
        max_steps: int = 12,
        max_file_chars: int = 20_000,
        max_call_seconds: float = 300.0,
        max_elapsed_seconds: float = 900.0,
        check_runner=None,
        execution_copy_stage: str | None = None,
    ):
        if not isinstance(root, Path):
            raise ValueError("root must be a pathlib.Path")
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise ValueError("root must be an existing directory")
        if not callable(chat):
            raise ValueError("chat must be callable")
        if check_runner is not None and not callable(check_runner):
            raise ValueError("check_runner must be callable or None")
        if type(max_steps) is not int or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        if type(max_file_chars) is not int or max_file_chars <= 0:
            raise ValueError("max_file_chars must be a positive integer")
        if (
            type(max_call_seconds) not in (int, float)
            or max_call_seconds <= 0
            or not math.isfinite(max_call_seconds)
        ):
            raise ValueError("max_call_seconds must be a positive number")
        if (
            type(max_elapsed_seconds) not in (int, float)
            or max_elapsed_seconds <= 0
            or not math.isfinite(max_elapsed_seconds)
        ):
            raise ValueError("max_elapsed_seconds must be a positive number")

        self.root = resolved_root
        self.chat = chat
        self.check_runner = check_runner
        if execution_copy_stage not in (None, "validator", "active"):
            raise ValueError("invalid execution copy stage")
        self.execution_copy_stage = execution_copy_stage
        self.max_steps = max_steps
        self.max_file_chars = max_file_chars
        self.max_call_seconds = float(max_call_seconds)
        self.max_elapsed_seconds = float(max_elapsed_seconds)
        self._builder_repair_feedback: str | None = None

    def set_builder_repair_feedback(self, detail: str) -> None:
        """Queue one trusted host-check finding for the next builder run."""
        valid_feedback = type(detail) is str and bool(detail)
        if not host_decision(HostOnlyDecision.REPAIR_FEEDBACK, valid_feedback):
            raise ValueError("repair feedback must be a non-empty string")
        if self._builder_repair_feedback is not None:
            raise ValueError("repair feedback is already pending")
        self._builder_repair_feedback = detail

    def __call__(
        self,
        role: str,
        task: SubagentTask,
        previous_results: tuple[SubagentResult, ...],
    ) -> SubagentResult:
        workspace = WorkspaceFileTools(self.root, task)
        feedback = self._builder_repair_feedback if role == "builder" else None
        try:
            return run_tool_loop(
                role=role,
                task=task,
                previous_results=previous_results,
                workspace=workspace,
                chat=self.chat,
                check_runner=self.check_runner,
                max_steps=self.max_steps,
                max_file_chars=self.max_file_chars,
                max_call_seconds=self.max_call_seconds,
                max_elapsed_seconds=self.max_elapsed_seconds,
                builder_repair_feedback=feedback,
                execution_copy_stage=self.execution_copy_stage,
            )
        finally:
            if role == "builder":
                self._builder_repair_feedback = None
