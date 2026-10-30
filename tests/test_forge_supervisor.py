import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.lifecycle_store import LifecycleStore
from src.forge_runtime.task_contract import Task
from src.forge_runtime.supervisor import (
    ForgeTaskExecutor, MinimalSupervisor, TaskOutcome,
)


def submitted(task_id="first", constraints=None):
    return Task.create(task_id, "Build the agreed greeting", constraints or {}, "chosen-model")


def wait_terminal(supervisor, task_id):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if supervisor.get_status(task_id) in {"completed", "failed"}:
            return supervisor.get_result(task_id)
        time.sleep(.01)
    raise AssertionError("Worker did not persist a terminal result")


class SupervisorTests(unittest.TestCase):
    def test_future_clock_snapshot_does_not_kill_worker_or_block_next_task(self):
        with tempfile.TemporaryDirectory() as folder:
            with MinimalSupervisor(LifecycleStore(Path(folder)), lambda task: TaskOutcome(True, {"value": 7})) as supervisor:
                future = Task.create("future", "Clock advanced before submit", {}, "chosen-model",
                                     now=datetime.now(timezone.utc) + timedelta(days=1))
                supervisor.submit_task(future)
                supervisor.submit_task(submitted("next"))
                supervisor.dispatch_task("future")
                supervisor.dispatch_task("next")
                self.assertEqual(wait_terminal(supervisor, "future")["status"], "completed")
                self.assertEqual(wait_terminal(supervisor, "next")["status"], "completed")
                saved = json.loads((Path(folder) / "future.json").read_text())
                self.assertGreaterEqual(saved["updated_at"], saved["created_at"])

    def test_mutated_executor_outcome_fails_task_and_keeps_worker_usable(self):
        calls = []
        def execute(task):
            calls.append(task.task_id)
            outcome = TaskOutcome(True, {"value": 1})
            if task.task_id == "first":
                outcome.result["value"] = object()
            return outcome
        with tempfile.TemporaryDirectory() as folder:
            with MinimalSupervisor(LifecycleStore(Path(folder)), execute) as supervisor:
                supervisor.submit_task(submitted())
                supervisor.submit_task(submitted("next"))
                supervisor.dispatch_task("first")
                supervisor.dispatch_task("next")
                report = wait_terminal(supervisor, "first")
                self.assertEqual(report["status"], "failed")
                self.assertIsNotNone(report["error"])
                self.assertEqual(wait_terminal(supervisor, "next")["status"], "completed")
                self.assertEqual(calls, ["first", "next"])

    def test_exception_with_invalid_utf8_message_is_persisted_without_worker_loss(self):
        def execute(task):
            raise RuntimeError("failure \ud800")
        with tempfile.TemporaryDirectory() as folder:
            with MinimalSupervisor(LifecycleStore(Path(folder)), execute) as supervisor:
                supervisor.submit_task(submitted())
                supervisor.dispatch_task("first")
                report = wait_terminal(supervisor, "first")
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error"]["kind"], "RuntimeError")
                report["error"]["message"].encode("utf-8", errors="strict")

    def test_status_and_attempt_are_persisted_before_executor_finishes(self):
        entered, release = threading.Event(), threading.Event()
        observed = []
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            def execute(task):
                observed.append(json.loads((Path(folder) / "first.json").read_text()))
                entered.set()
                release.wait(3)
                return TaskOutcome(True, {"value": 7})
            with MinimalSupervisor(store, execute) as supervisor:
                supervisor.submit_task(submitted())
                supervisor.dispatch_task("first")
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(supervisor.get_status("first"), "running")
                    self.assertEqual(observed[0]["attempt_count"], 1)
                    self.assertEqual(observed[0]["status"], "running")
                    self.assertIsNotNone(observed[0]["started_at"])
                finally:
                    release.set()
                report = wait_terminal(supervisor, "first")
                self.assertEqual(report["status"], "completed")
                self.assertEqual(report["result"], {"value": 7})
                disk = json.loads((Path(folder) / "first.json").read_text())
                self.assertEqual(disk["result"], {"value": 7})
                self.assertIsNotNone(disk["completed_at"])

    def test_worker_runs_tasks_serially_and_rejects_duplicate_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def execute(task):
            calls.append(task.task_id)
            if task.task_id == "first":
                entered.set()
                release.wait(3)
            return TaskOutcome(True, {"task": task.task_id})
        with tempfile.TemporaryDirectory() as folder:
            with MinimalSupervisor(LifecycleStore(Path(folder)), execute) as supervisor:
                supervisor.submit_task(submitted())
                supervisor.submit_task(submitted("second"))
                supervisor.dispatch_task("first")
                try:
                    self.assertTrue(entered.wait(2))
                    supervisor.dispatch_task("second")
                    with self.assertRaises(RuntimeError):
                        supervisor.dispatch_task("first")
                    self.assertEqual(supervisor.get_status("second"), "queued")
                    self.assertEqual(calls, ["first"])
                finally:
                    release.set()
                wait_terminal(supervisor, "first")
                wait_terminal(supervisor, "second")
                self.assertEqual(calls, ["first", "second"])
                with self.assertRaises(RuntimeError):
                    supervisor.dispatch_task("first")

    def test_rejected_build_and_exception_are_saved_as_failed_without_retry(self):
        for outcome in (TaskOutcome(False, error={"kind": "rejected", "message": "Host denied"}),
                        ValueError("executor failed")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                calls = []
                def execute(task):
                    calls.append(task.task_id)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                with MinimalSupervisor(LifecycleStore(Path(folder)), execute) as supervisor:
                    supervisor.submit_task(submitted())
                    supervisor.dispatch_task("first")
                    report = wait_terminal(supervisor, "first")
                    self.assertEqual(report["status"], "failed")
                    self.assertIsNotNone(report["error"])
                    self.assertEqual(calls, ["first"])
                    self.assertEqual(json.loads((Path(folder) / "first.json").read_text())["attempt_count"], 1)

    def test_running_record_is_failed_on_reopen_without_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            with store.exclusive():
                original = submitted()
                store.create(original)
                now = datetime.now(timezone.utc)
                store.save(replace(original, status="running", attempt_count=1,
                                   started_at=now, updated_at=now))
            calls = []
            with MinimalSupervisor(LifecycleStore(Path(folder)), lambda task: calls.append(task)) as supervisor:
                report = supervisor.get_result("first")
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error"]["kind"], "interrupted")
                self.assertEqual(calls, [])
                with self.assertRaises(RuntimeError):
                    supervisor.dispatch_task("first")

    def test_second_supervisor_is_denied_and_result_snapshots_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            with MinimalSupervisor(LifecycleStore(Path(folder)), lambda task: TaskOutcome(True, {"items": [1]})) as supervisor:
                with self.assertRaises(RuntimeError):
                    MinimalSupervisor(LifecycleStore(Path(folder)), lambda task: None)
                task = submitted(constraints={"nested": [1]})
                saved = supervisor.submit_task(task)
                saved.constraints["nested"].append(99)
                supervisor.dispatch_task("first")
                result = wait_terminal(supervisor, "first")
                result["result"]["items"].append(99)
                self.assertEqual(supervisor.get_result("first")["result"], {"items": [1]})
                self.assertEqual(json.loads((Path(folder) / "first.json").read_text())["constraints"], {"nested": [1]})

    def test_caller_cannot_save_a_forged_success(self):
        with tempfile.TemporaryDirectory() as folder:
            with MinimalSupervisor(LifecycleStore(Path(folder)), lambda task: self.fail("Executed")) as supervisor:
                task = supervisor.submit_task(submitted())
                now = datetime.now(timezone.utc)
                forged = replace(task, status="completed", attempt_count=1,
                                 started_at=now, completed_at=now, updated_at=now, result={"approved": True})
                with self.assertRaises(ValueError):
                    supervisor.save_state(forged)
                self.assertEqual(supervisor.get_status("first"), "queued")

    def test_start_storage_failure_never_calls_executor_or_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            supervisor = MinimalSupervisor(store, lambda task: self.fail("Executed despite failed start save"))
            supervisor.submit_task(submitted())
            original_save = store.save
            failed = threading.Event()
            def save(task):
                if task.status == "running":
                    failed.set()
                    raise OSError("disk full")
                return original_save(task)
            try:
                with patch.object(store, "save", side_effect=save):
                    supervisor.dispatch_task("first")
                    self.assertTrue(failed.wait(2))
                    # get_status waits for the worker's state lock and sees uncertainty.
                    with self.assertRaises(RuntimeError):
                        supervisor.get_status("first")
                self.assertEqual(json.loads((Path(folder) / "first.json").read_text())["status"], "queued")
            finally:
                supervisor.close()
            self.assertTrue(store.lock_path.exists())

    def test_postreplace_readback_validation_failure_blocks_followup_execution(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            supervisor = MinimalSupervisor(store, lambda task: self.fail("Executed with uncertain storage"))
            try:
                with patch("src.forge_runtime.lifecycle_store._read_bytes",
                           side_effect=ValueError("file changed during readback")):
                    with self.assertRaises(ValueError):
                        supervisor.submit_task(submitted())
                self.assertTrue((Path(folder) / "first.json").is_file())
                with self.assertRaises(RuntimeError):
                    supervisor.submit_task(submitted("next"))
            finally:
                supervisor.close()
            self.assertTrue(store.lock_path.exists())

    def test_save_state_uses_one_snapshot_when_caller_mutates_during_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            with MinimalSupervisor(store, lambda task: TaskOutcome(True, {"value": 7})) as supervisor:
                saved = supervisor.submit_task(submitted(constraints={"nested": [1]}))
                original_save = store.save
                def save(task):
                    original_save(task)
                    saved.constraints["nested"].append(99)
                with patch.object(store, "save", side_effect=save):
                    returned = supervisor.save_state(saved)
                self.assertEqual(returned.constraints, {"nested": [1]})
                self.assertEqual(json.loads((Path(folder) / "first.json").read_text())["constraints"], {"nested": [1]})
                supervisor.dispatch_task("first")
                self.assertEqual(wait_terminal(supervisor, "first")["status"], "completed")

    def test_final_storage_failure_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            supervisor = MinimalSupervisor(store, lambda task: TaskOutcome(True, {"value": 7}))
            supervisor.submit_task(submitted())
            original_save = store.save
            failed = threading.Event()
            def save(task):
                if task.status == "completed":
                    failed.set()
                    raise OSError("disk full")
                return original_save(task)
            try:
                with patch.object(store, "save", side_effect=save):
                    supervisor.dispatch_task("first")
                    self.assertTrue(failed.wait(2))
                    with self.assertRaises(RuntimeError):
                        supervisor.get_result("first")
                self.assertEqual(json.loads((Path(folder) / "first.json").read_text())["status"], "running")
            finally:
                supervisor.close()
            self.assertTrue(store.lock_path.exists())

    def test_close_keeps_exclusion_until_running_executor_has_ended(self):
        entered, release = threading.Event(), threading.Event()
        def execute(task):
            entered.set()
            release.wait(3)
            return TaskOutcome(True, {"value": 7})
        with tempfile.TemporaryDirectory() as folder:
            store = LifecycleStore(Path(folder))
            supervisor = MinimalSupervisor(store, execute)
            supervisor.submit_task(submitted())
            supervisor.dispatch_task("first")
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(RuntimeError):
                    supervisor.close(timeout=.01)
                self.assertTrue(store.lock_path.exists())
                with self.assertRaises(RuntimeError):
                    MinimalSupervisor(LifecycleStore(Path(folder)), execute)
            finally:
                release.set()
                supervisor.close()


class ForgeExecutorTests(unittest.TestCase):
    def test_adapter_uses_chosen_model_and_actual_hostchecks(self):
        for code, expected in (("def greeting(name):\n    return 'Hello, ' + name\n", "completed"),
                               ("def greeting(name):\n    return 'Wrong'\n", "failed")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "build").mkdir()
                (root / "state").mkdir()
                spec = json.loads((Path(__file__).parents[1] / "examples/hello_agent.json").read_text())
                models = []
                role_started = set()
                def chat(messages):
                    role = json.loads(messages[1][1])["role"]
                    if role == "builder":
                        if role not in role_started:
                            role_started.add(role)
                            return json.dumps({"action": "write_file", "path": "agent/hello.py", "content": code})
                        return json.dumps({"action": "finish", "passed": True, "summary": "Built greeting", "findings": []})
                    if role not in role_started:
                        role_started.add(role)
                        return json.dumps({"action": "run_check"})
                    return json.dumps({"action": "finish", "passed": expected == "completed", "summary": "Checked greeting", "findings": []})
                def factory(model_id):
                    models.append(model_id)
                    return chat
                task = Task.create(spec["task_id"], spec["objective"],
                                   {"workspace": "build", "task_spec": spec}, "chosen-model")
                with MinimalSupervisor(LifecycleStore(root / "state"), ForgeTaskExecutor(root, factory)) as supervisor:
                    supervisor.submit_task(task)
                    supervisor.dispatch_task(task.task_id)
                    report = wait_terminal(supervisor, task.task_id)
                    self.assertEqual(report["status"], expected)
                    self.assertEqual(models, ["chosen-model"])
                    if expected == "completed":
                        self.assertTrue(report["result"]["host_passed"])
                        self.assertTrue(report["result"]["pipeline_approved"])
                        self.assertEqual((root / "build/agent/hello.py").read_text(), code)

    def test_adapter_rejects_task_and_path_changes_before_model_contact(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "build").mkdir()
            spec = json.loads((Path(__file__).parents[1] / "examples/hello_agent.json").read_text())
            executor = ForgeTaskExecutor(root, lambda model: self.fail("Model contacted"))
            for task in (Task.create(spec["task_id"], "Altered objective", {"workspace": "build", "task_spec": spec}, "chosen-model"),
                         Task.create(spec["task_id"], spec["objective"], {"workspace": "../outside", "task_spec": spec}, "chosen-model")):
                with self.subTest(task=task):
                    with self.assertRaises((ValueError, OSError)):
                        executor(task)


if __name__ == "__main__":
    unittest.main()
