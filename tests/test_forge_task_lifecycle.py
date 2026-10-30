"""Behavioral acceptance of the Task V1 contract and durable lifecycle store."""

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.task_contract import Task
from src.forge_runtime.lifecycle_store import LifecycleStore


NOW = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)


def queued(task_id="task-1", **kwargs):
    kwargs.setdefault("now", NOW)
    return Task.create(task_id, "Build a greeting", {"limits": [1, {"host": True}]},
                       "test-model", **kwargs)


def running(task):
    return replace(task, status="running", attempt_count=1,
                   started_at=NOW + timedelta(seconds=1),
                   updated_at=NOW + timedelta(seconds=1))


def completed(task):
    return replace(task, status="completed", result={"passed": True, "files": ["hello.py"]},
                   completed_at=NOW + timedelta(seconds=2),
                   updated_at=NOW + timedelta(seconds=2))


class TaskContractTests(unittest.TestCase):
    def test_roundtrip_preserves_utc_fields_and_completion_evidence(self):
        task = completed(running(queued()))
        encoded = json.loads(json.dumps(task.to_dict()))
        self.assertEqual(encoded["schema_version"], 1)
        self.assertEqual(encoded["created_at"], "2026-10-13T10:00:00+00:00")
        restored = Task.from_dict(encoded)
        self.assertEqual(restored.status, "completed")
        self.assertEqual(restored.result, {"passed": True, "files": ["hello.py"]})
        self.assertEqual(restored.attempt_count, 1)
        self.assertEqual(restored.completed_at, NOW + timedelta(seconds=2))
        self.assertEqual(restored.created_at.utcoffset(), timedelta(0))

    def test_inputs_and_returned_snapshots_do_not_alias_task(self):
        source = {"steps": [{"name": "build"}]}
        task = Task.create("copy", "Build", source, "model", now=NOW)
        source["steps"][0]["name"] = "changed"
        snapshot = task.to_dict()
        snapshot["constraints"]["steps"][0]["name"] = "changed-again"
        restored = Task.from_dict(task.to_dict())
        self.assertEqual(restored.constraints, {"steps": [{"name": "build"}]})
        with self.assertRaises(FrozenInstanceError):
            task.status = "running"

    def test_from_dict_does_not_mutate_or_keep_nested_input(self):
        data = queued().to_dict()
        original = json.loads(json.dumps(data))
        task = Task.from_dict(data)
        self.assertEqual(data, original)
        data["constraints"]["limits"][1]["host"] = False
        self.assertTrue(task.constraints["limits"][1]["host"])

    def test_rejects_unknown_version_fields_and_unsafe_ids(self):
        for changes in ({"schema_version": 2}, {"schema_version": True},
                        {"task_id": "../outside"}, {"task_id": "CON"},
                        {"task_id": "trailing."}, {"task_id": "a" * 65},
                        {"task_id": ""}, {"unknown": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Task.from_dict(queued().to_dict() | changes)

    def test_rejects_naive_non_utc_and_inconsistent_timestamps(self):
        for changes in ({"created_at": "2026-10-13T10:00:00"},
                        {"created_at": "2026-10-13T10:00:00+01:00"},
                        {"updated_at": "2026-10-13T09:59:59+00:00"},
                        {"started_at": "2026-10-13T10:00:00+00:00"},
                        {"completed_at": "not-a-time"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Task.from_dict(queued().to_dict() | changes)
        with self.assertRaises(ValueError):
            queued(now=datetime(2026, 10, 13))

    def test_counters_and_terminal_evidence_are_validated(self):
        for changes in ({"max_attempts": 0}, {"max_attempts": True},
                        {"attempt_count": -1}, {"attempt_count": 2},
                        {"attempt_count": True}, {"status": "unknown"},
                        {"status": "completed"}, {"status": "failed"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Task.from_dict(queued().to_dict() | changes)
        for changes in ({"result": None}, {"completed_at": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Task.from_dict(completed(running(queued())).to_dict() | changes)

    def test_json_values_must_be_finite_and_strictly_serializable(self):
        for value in (float("nan"), float("inf"), {1: "not-a-string-key"},
                      {"set": {1, 2}}, {"object": object()}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Task.create("finite", "Build", {"input": value}, "model", now=NOW)
        circular = []
        circular.append(circular)
        with self.assertRaises(ValueError):
            Task.create("cycle", "Build", {"input": circular}, "model", now=NOW)


class LifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LifecycleStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_mutations_require_exclusive_session(self):
        with self.assertRaises(RuntimeError):
            self.store.create(queued())
        with self.store.exclusive():
            self.store.create(queued())
        with self.assertRaises(RuntimeError):
            self.store.save(running(queued()))
        self.assertEqual(json.loads((self.root / "task-1.json").read_text())["status"], "queued")

    def test_roundtrip_and_list_are_durable_independent_snapshots(self):
        with self.store.exclusive():
            self.store.create(queued("b"))
            self.store.create(queued("a"))
            saved = self.store.load("b")
            saved.constraints["limits"][1]["host"] = False
            self.assertTrue(self.store.load("b").constraints["limits"][1]["host"])
            self.assertEqual([task.task_id for task in self.store.list_tasks()], ["a", "b"])
        with LifecycleStore(self.root).exclusive() as reopened:
            self.assertEqual(reopened.load("a").objective, "Build a greeting")

    def test_duplicate_id_never_replaces_existing_task(self):
        with self.store.exclusive():
            self.store.create(queued())
            before = (self.root / "task-1.json").read_bytes()
            with self.assertRaises(FileExistsError):
                self.store.create(replace(queued(), objective="Other objective"))
            self.assertEqual((self.root / "task-1.json").read_bytes(), before)

    def test_one_pid_lock_and_no_automatic_stale_lock_removal(self):
        with self.store.exclusive():
            self.assertIn(str(os.getpid()), self.store.lock_path.read_text())
            with self.assertRaises(RuntimeError):
                with LifecycleStore(self.root).exclusive():
                    self.fail("A second controlling session entered")
        self.assertFalse(self.store.lock_path.exists())
        self.store.lock_path.write_text("2147483647")
        with self.assertRaises(RuntimeError):
            with LifecycleStore(self.root).exclusive():
                self.fail("Stale PID lock must be checked manually")
        self.assertEqual(self.store.lock_path.read_text(), "2147483647")

    def test_uncertain_session_can_retain_lock_on_close(self):
        with self.store.exclusive():
            self.store.retain_lock_on_close = True
        self.assertTrue(self.store.lock_path.exists())
        with self.assertRaises(RuntimeError):
            with LifecycleStore(self.root).exclusive():
                self.fail("Uncertain lifecycle state must remain locked")

    def test_a_different_process_cannot_use_an_inherited_store_session(self):
        owner = os.getpid()
        with self.store.exclusive():
            self.store.create(queued())
            with patch("src.forge_runtime.lifecycle_store.os.getpid", return_value=owner + 1):
                with self.assertRaises(RuntimeError):
                    self.store.save(running(queued()))
            self.assertEqual(self.store.load("task-1").status, "queued")

    def test_transitions_and_attempt_are_persisted_together(self):
        with self.store.exclusive():
            task = queued(max_attempts=2)
            self.store.create(task)
            self.store.save(running(task))
            actual = self.store.load(task.task_id)
            self.assertEqual((actual.status, actual.attempt_count), ("running", 1))
            self.store.save(completed(actual))
            self.assertTrue(self.store.load(task.task_id).result["passed"])

    def test_queued_cancellation_and_interrupted_running_failure_are_terminal(self):
        with self.store.exclusive():
            first = queued("cancel")
            self.store.create(first)
            self.store.save(replace(first, status="cancelled", completed_at=NOW, updated_at=NOW))
            second = running(queued("interrupt"))
            self.store.create(queued("interrupt"))
            self.store.save(second)
            failed = replace(second, status="failed", error={"kind": "interrupted"},
                             completed_at=NOW + timedelta(seconds=3),
                             updated_at=NOW + timedelta(seconds=3))
            self.store.save(failed)
            self.assertEqual(self.store.load("cancel").attempt_count, 0)
            self.assertEqual(self.store.load("interrupt").error, {"kind": "interrupted"})

    def test_create_requires_pristine_queued_snapshot(self):
        with self.store.exclusive(), self.assertRaises(ValueError):
            self.store.create(running(queued()))
        self.assertFalse((self.root / "task-1.json").exists())

    def test_forbidden_transition_and_counter_change_preserve_file(self):
        with self.store.exclusive():
            task = queued(max_attempts=3)
            self.store.create(task)
            before = (self.root / "task-1.json").read_bytes()
            for candidate in (completed(running(task)), replace(running(task), attempt_count=2)):
                with self.subTest(status=candidate.status), self.assertRaises(ValueError):
                    self.store.save(candidate)
                self.assertEqual((self.root / "task-1.json").read_bytes(), before)
            current = running(task)
            self.store.save(current)
            with self.assertRaises(ValueError):
                self.store.save(task)
            with self.assertRaises(ValueError):
                self.store.save(replace(completed(current), attempt_count=2))

    def test_immutable_contract_fields_cannot_be_changed(self):
        with self.store.exclusive():
            task = queued()
            self.store.create(task)
            for changes in ({"objective": "Other"}, {"constraints": {}},
                            {"model_id": "other-model"}, {"max_attempts": 2},
                            {"created_at": NOW - timedelta(seconds=1)}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.store.save(replace(task, **changes))
            self.assertEqual(self.store.load("task-1").objective, "Build a greeting")

    def test_same_status_only_allows_monotonic_timestamp(self):
        with self.store.exclusive():
            task = queued()
            self.store.create(task)
            later = replace(task, updated_at=NOW + timedelta(seconds=1))
            self.store.save(later)
            with self.assertRaises(ValueError):
                self.store.save(task)
            self.store.save(running(later))
            done = completed(running(later))
            self.store.save(done)
            self.store.save(replace(done, updated_at=NOW + timedelta(seconds=3)))
            with self.assertRaises(ValueError):
                self.store.save(replace(done, result={"passed": False},
                                        updated_at=NOW + timedelta(seconds=4)))

    def test_start_cannot_precede_the_last_persisted_snapshot(self):
        with self.store.exclusive():
            task = queued()
            self.store.create(task)
            self.store.save(replace(task, updated_at=NOW + timedelta(seconds=3)))
            stale_start = replace(running(task), updated_at=NOW + timedelta(seconds=4))
            with self.assertRaises(ValueError):
                self.store.save(stale_start)
            self.assertEqual(self.store.load("task-1").status, "queued")

    def test_completion_cannot_precede_the_last_persisted_snapshot(self):
        with self.store.exclusive():
            task = queued()
            self.store.create(task)
            self.store.save(running(task))
            self.store.save(replace(running(task), updated_at=NOW + timedelta(seconds=3)))
            stale_end = replace(completed(running(task)), updated_at=NOW + timedelta(seconds=4))
            with self.assertRaises(ValueError):
                self.store.save(stale_end)
            self.assertEqual(self.store.load("task-1").status, "running")

    def test_fsync_failure_preserves_previous_state_and_cleans_tempfile(self):
        with self.store.exclusive():
            self.store.create(queued())
            before = (self.root / "task-1.json").read_bytes()
            with patch("src.forge_runtime.lifecycle_store.os.fsync", side_effect=OSError("disk flush refused")):
                with self.assertRaises(OSError):
                    self.store.save(running(queued()))
            self.assertEqual((self.root / "task-1.json").read_bytes(), before)
            self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                             sorted(["task-1.json", self.store.lock_path.name]))

    def test_replace_failure_preserves_previous_state_and_cleans_tempfile(self):
        with self.store.exclusive():
            self.store.create(queued())
            with patch("src.forge_runtime.lifecycle_store.os.replace", side_effect=OSError("disk refused")):
                with self.assertRaises(OSError):
                    self.store.save(running(queued()))
            self.assertEqual(self.store.load("task-1").status, "queued")
            self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                             sorted(["task-1.json", self.store.lock_path.name]))

    def test_readback_failure_reports_ambiguous_save_instead_of_success(self):
        with self.store.exclusive():
            self.store.create(queued())
            original_replace = os.replace

            def replace_and_corrupt(source, target):
                original_replace(source, target)
                Path(target).write_text('{"unexpected": true}')

            with patch("src.forge_runtime.lifecycle_store.os.replace", side_effect=replace_and_corrupt):
                with self.assertRaises((OSError, ValueError)):
                    self.store.save(running(queued()))
            self.assertEqual(json.loads((self.root / "task-1.json").read_text()), {"unexpected": True})

    def test_load_rejects_schema_filename_mismatch_duplicate_keys_and_oversize(self):
        with self.store.exclusive():
            self.store.create(queued())
            path = self.root / "task-1.json"
            data = queued().to_dict()
            path.write_text(json.dumps(data | {"task_id": "other"}))
            with self.assertRaises(ValueError):
                self.store.load("task-1")
            text = json.dumps(data).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
            path.write_text(text)
            with self.assertRaises(ValueError):
                self.store.load("task-1")
            path.write_bytes(b" " * 1_000_001)
            with self.assertRaises(ValueError):
                self.store.load("task-1")

    def test_links_are_rejected_before_external_file_can_be_changed(self):
        outside = self.root / "outside.json"
        outside.write_text("preserved")
        target = self.root / "task-1.json"
        try:
            target.symlink_to(outside)
        except OSError:
            self.skipTest("OS does not permit creating a test symlink")
        with self.store.exclusive():
            with self.assertRaises(ValueError):
                self.store.load("task-1")
            with self.assertRaises(ValueError):
                self.store.create(queued())
        self.assertEqual(outside.read_text(), "preserved")

    def test_hardlinked_task_is_rejected_without_modifying_either_name(self):
        outside = self.root / "source.txt"
        outside.write_text(json.dumps(queued().to_dict()))
        target = self.root / "task-1.json"
        os.link(outside, target)
        before = outside.read_bytes()
        with self.store.exclusive():
            with self.assertRaises(ValueError):
                self.store.load("task-1")
            with self.assertRaises(ValueError):
                self.store.save(running(queued()))
        self.assertEqual(outside.read_bytes(), before)
        self.assertEqual(target.read_bytes(), before)

    def test_store_rejects_linked_directory_without_creating_lock(self):
        alias = self.root / "linked-directory"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit creating a test directory symlink")
        with self.assertRaises(ValueError):
            LifecycleStore(alias)
        self.assertFalse(self.store.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
