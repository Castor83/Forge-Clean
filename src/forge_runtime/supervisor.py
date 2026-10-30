"""One host-controlled task worker; persistence is not process isolation."""

import copy
import json
import math
import queue
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .lifecycle_store import LifecycleStore
from .task_contract import Task
from .task_runner import parse_task_spec, run_forge_task, verify_forge_task


@dataclass(frozen=True)
class TaskOutcome:
    approved: bool
    result: dict | None = None
    error: dict | None = None

    def __post_init__(self):
        if type(self.approved) is not bool:
            raise ValueError("approved must be a host boolean")
        if self.result is not None and type(self.result) is not dict:
            raise ValueError("result must be a dictionary or None")
        if self.error is not None and type(self.error) is not dict:
            raise ValueError("error must be a dictionary or None")
        if self.approved and (self.result is None or self.error is not None):
            raise ValueError("approved outcome requires a result and no error")
        if not self.approved and (not self.error or any(
                type(self.error.get(key)) is not str or not self.error[key].strip()
                for key in ("kind", "message"))):
            raise ValueError("failed outcome requires an error kind and message")
        # Task validation rejects non-JSON values and performs deep snapshotting.
        probe = Task.create("outcome", "Validate outcome", {}, "host")
        now = max(datetime.now(timezone.utc), probe.updated_at)
        replace(probe, status="completed" if self.approved else "failed",
                attempt_count=1, started_at=now, completed_at=now, updated_at=now,
                result=self.result, error=self.error)
        object.__setattr__(self, "result", copy.deepcopy(self.result))
        object.__setattr__(self, "error", copy.deepcopy(self.error))


class MinimalSupervisor:
    """Persist before execution and completion; never retry a terminal task."""

    def __init__(self, store: LifecycleStore, executor):
        if type(store) is not LifecycleStore or not callable(executor):
            raise ValueError("supervisor requires a LifecycleStore and host executor")
        self.store = store
        self._executor = executor
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._scheduled = set()
        self._records = {}
        self._uncertain = None
        self._closed = False
        self._stop = threading.Event()
        self._session = store.exclusive()
        self._session.__enter__()
        try:
            for task in store.list_tasks():
                self._records[task.task_id] = task
                if task.status == "running":
                    now = max(datetime.now(timezone.utc), task.updated_at)
                    self._persist(replace(task, status="failed", updated_at=now,
                                          completed_at=now, result=None,
                                          error={"kind": "interrupted", "message":
                                                 "Previous execution ended without a persisted result; no retry started."}))
        except BaseException:
            self._session.__exit__(None, None, None)
            raise
        self._worker = threading.Thread(target=self._work, name="forge-task-worker", daemon=True)
        self._worker.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def submit_task(self, task: Task) -> Task:
        with self._lock:
            self._available()
            if self._stop.is_set() or type(task) is not Task:
                raise ValueError("submit requires an open supervisor and Task")
            snapshot = Task.from_dict(task.to_dict())
            if snapshot.status != "queued" or snapshot.attempt_count != 0:
                raise ValueError("submit accepts only fresh queued tasks")
            try:
                self.store.create(snapshot)
            except FileExistsError:
                raise
            except BaseException:
                self._poison("Task submission could not be persisted reliably")
                raise
            self._records[snapshot.task_id] = snapshot
            return Task.from_dict(snapshot.to_dict())

    def dispatch_task(self, task_id: str) -> None:
        with self._lock:
            task = self._record(task_id)
            if self._stop.is_set() or task.status != "queued" or task_id in self._scheduled:
                raise RuntimeError("task cannot be dispatched again or after shutdown")
            self._scheduled.add(task_id)
            self._queue.put(task_id)

    def get_status(self, task_id: str) -> str:
        with self._lock:
            return self._record(task_id).status

    def save_state(self, task: Task) -> Task:
        """Flush a known host snapshot; callers cannot forge state transitions."""
        with self._lock:
            if type(task) is not Task:
                raise ValueError("save_state requires a Task")
            snapshot = Task.from_dict(task.to_dict())
            current = self._record(snapshot.task_id)
            if self._stop.is_set() or snapshot.to_dict() != current.to_dict():
                raise ValueError("state changes are owned by the supervisor worker")
            self._persist(snapshot)
            return Task.from_dict(snapshot.to_dict())

    def get_result(self, task_id: str) -> dict:
        with self._lock:
            task = self._record(task_id)
            return copy.deepcopy({"status": task.status, "result": task.result, "error": task.error})

    def close(self, timeout: float = 5.0) -> None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("close timeout must be finite and nonnegative")
        with self._lock:
            if self._closed:
                return
            self._stop.set()
        self._worker.join(timeout)
        if self._worker.is_alive():
            # Keep the exclusive session until the running executor truly ends.
            raise RuntimeError("worker still running; session lock remains held")
        with self._lock:
            self._closed = True
            self._session.__exit__(None, None, None)

    def _available(self):
        if self._uncertain is not None:
            raise RuntimeError(self._uncertain)
        if self._closed:
            raise RuntimeError("supervisor is closed")

    def _record(self, task_id):
        self._available()
        # Validate IDs even on lookups; never use an untrusted value as a path.
        if type(task_id) is not str or task_id not in self._records:
            raise ValueError("unknown task")
        return self._records[task_id]

    def _poison(self, message):
        self._uncertain = message + "; storage uncertain, execution blocked and lock retained"
        self.store.retain_lock_on_close = True
        self._stop.set()

    def _persist(self, task):
        try:
            snapshot = Task.from_dict(task.to_dict())
            self.store.save(snapshot)
        except BaseException:
            self._poison("Task state could not be persisted reliably")
            raise
        self._records[snapshot.task_id] = snapshot

    def _work(self):
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                self._run_one(task_id)
            except BaseException:
                with self._lock:
                    self._poison("Unexpected task worker failure")
            finally:
                self._queue.task_done()

    def _run_one(self, task_id):
        with self._lock:
            if self._stop.is_set():
                return
            task = self._record(task_id)
            now = max(datetime.now(timezone.utc), task.updated_at)
            running = replace(task, status="running", attempt_count=task.attempt_count + 1,
                              started_at=now, updated_at=now)
            try:
                self._persist(running)
            except BaseException:
                return
        try:
            outcome = self._executor(Task.from_dict(running.to_dict()))
            if type(outcome) is not TaskOutcome:
                raise ValueError("host executor returned an invalid outcome")
            # The executor may have mutated nested values after constructing it.
            outcome = TaskOutcome(outcome.approved, outcome.result, outcome.error)
        except BaseException as exc:
            try:
                message = str(exc) or "Executor interrupted"
            except BaseException:
                message = "Executor failed; exception message unavailable"
            message = message.encode("utf-8", errors="backslashreplace").decode("utf-8")
            outcome = TaskOutcome(False, error={"kind": type(exc).__name__, "message": message})
        with self._lock:
            now = max(datetime.now(timezone.utc), running.updated_at)
            terminal = replace(running, status="completed" if outcome.approved else "failed",
                               updated_at=now, completed_at=now,
                               result=outcome.result, error=outcome.error)
            try:
                self._persist(terminal)
            except BaseException:
                return
            self._scheduled.discard(task_id)


class ForgeTaskExecutor:
    """Adapt a host task to the unchanged restricted Forge build/check paths."""

    def __init__(self, repository_root: Path, chat_factory):
        self.root = repository_root.resolve(strict=True)
        if not self.root.is_dir() or not callable(chat_factory):
            raise ValueError("Forge adapter needs a repository root and model client factory")
        self.chat_factory = chat_factory

    def __call__(self, task: Task) -> TaskOutcome:
        if type(task) is not Task or set(task.constraints) != {"workspace", "task_spec"}:
            raise ValueError("Forge task requires only workspace and task_spec constraints")
        spelling = task.constraints["workspace"]
        if type(spelling) is not str or not spelling or Path(spelling).is_absolute():
            raise ValueError("workspace must be a relative repository directory")
        workspace = (self.root / spelling).resolve(strict=True)
        try:
            workspace.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("workspace must stay inside the repository") from exc
        if not workspace.is_dir():
            raise ValueError("workspace must be a directory")
        spec = parse_task_spec(json.dumps(task.constraints["task_spec"], allow_nan=False))
        if spec.task.task_id != task.task_id or spec.task.objective != task.objective:
            raise ValueError("Task identity and objective must match the bound Forge specification")
        chat = self.chat_factory(task.model_id)
        result = run_forge_task(workspace, spec, chat, max_steps=20,
                                max_call_seconds=300.0, max_elapsed_seconds=900.0,
                                max_repair_attempts=1)
        passed, detail = verify_forge_task(workspace, spec)
        payload = {"pipeline_approved": result.approved, "host_passed": passed,
                   "host_detail": detail, "roles": [
                       {"role": role.role, "passed": role.passed, "summary": role.summary,
                        "findings": list(role.findings)} for role in result.results]}
        if result.approved and passed:
            return TaskOutcome(True, payload)
        return TaskOutcome(False, payload,
                           {"kind": "build_rejected", "message": detail or "Forge roles rejected the build"})
