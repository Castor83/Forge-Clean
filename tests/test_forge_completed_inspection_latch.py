"""Completed role checks stay closed across invalid finish corrections."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import task_runner
from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_build_context_contract import ConfiguredClient
from tests.test_forge_conversation_build import ready


PATH = "agent/hello.py"
SOURCE = "def greeting(name):\n    return 'Hallo ' + name\n"


def finish(passed=True):
    return {"action": "finish", "passed": passed, "summary": "Checked" if passed else "Rejected",
            "findings": [] if passed else ["Explicit role rejection"]}


def initial_builder(source=SOURCE):
    return [{"action": "write_file", "path": PATH, "content": source}, finish()]


def review_steps(*, invalid_finish=False, passed=True):
    values = [{"action": "read_file", "path": PATH}, {"action": "run_check"}]
    if invalid_finish:
        values.append({"action": "finish"})
    values.append(finish(passed))
    return values


def client_for(values):
    responses = iter(values)
    return ConfiguredClient(lambda _: json.dumps(next(responses)))


class CompletedInspectionLatchTests(unittest.TestCase):
    def prepare(self, store):
        task = ConversationTask(store)
        task.initialize_project("inspection_latch")
        ready(task)
        return task

    def test_invalid_finish_cannot_reopen_read_or_check_in_tester_or_reviewer(self):
        original_check = task_runner._StaticCheckRunner.__call__
        for role in ("tester", "reviewer"):
            for following_action in ({"action": "read_file", "path": PATH}, {"action": "run_check"}):
                with self.subTest(role=role, action=following_action["action"]), \
                        tempfile.TemporaryDirectory() as folder:
                    values = initial_builder()
                    if role == "reviewer":
                        values.extend(review_steps())
                    values.extend([{"action": "run_check"}, {"action": "finish"}, following_action, finish()])
                    if role == "tester":
                        values.extend(review_steps())
                    client = client_for(values)
                    with TaskStore(Path(folder)).exclusive() as store:
                        task = self.prepare(store)
                        with patch.object(task_runner._StaticCheckRunner, "__call__", autospec=True,
                                          side_effect=original_check) as check:
                            with self.assertRaises(ValueError):
                                build_confirmed(task, client, lambda _: None)
                        self.assertEqual(check.call_count, 1 if role == "tester" else 2)
                        self.assertEqual(client.calls[-1]["role"], role)
                        self.assertEqual(client.calls[-1]["feedback"]["error"], "invalid_tool_action")
                        self.assertEqual(client.calls[-1]["actions"], ("finish",))
                        self.assertEqual(task.data["state"], "failed")
                        self.assertFalse(task.data["report"].get("approved", False))

    def test_corrected_negative_finish_remains_allowed_in_both_review_roles(self):
        original_check = task_runner._StaticCheckRunner.__call__
        for rejected_role in ("tester", "reviewer"):
            with self.subTest(role=rejected_role), tempfile.TemporaryDirectory() as folder:
                values = initial_builder()
                if rejected_role == "reviewer":
                    values.extend(review_steps())
                values.extend(review_steps(invalid_finish=True, passed=False))
                client = client_for(values)
                with TaskStore(Path(folder)).exclusive() as store:
                    task = self.prepare(store)
                    with patch.object(task_runner._StaticCheckRunner, "__call__", autospec=True,
                                      side_effect=original_check) as check:
                        self.assertFalse(build_confirmed(task, client, lambda _: None))
                    # The final independent check is still required and remains positive.
                    self.assertEqual(check.call_count, 2 if rejected_role == "tester" else 3)
                    self.assertEqual(client.calls[-1]["role"], rejected_role)
                    self.assertEqual(client.calls[-1]["feedback"]["error"], "invalid_tool_action")
                    self.assertEqual(client.calls[-1]["actions"], ("finish",))
                    self.assertTrue(task.data["report"]["host_passed"])
                    self.assertFalse(task.data["report"]["pipeline_approved"])
                    self.assertFalse(task.data["report"]["approved"])
                    self.assertFalse(task.data["report"]["results"][-1]["passed"])

    def test_successful_corrected_finishes_reset_permissions_for_the_next_role(self):
        values = initial_builder() + review_steps(invalid_finish=True) + review_steps(invalid_finish=True)
        client = client_for(values)
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.prepare(store)
                self.assertTrue(build_confirmed(task, client, lambda _: None))
                for role in ("tester", "reviewer"):
                    calls = [call for call in client.calls if call["role"] == role]
                    self.assertIn("read_file", calls[0]["actions"])
                    self.assertIn("run_check", calls[0]["actions"])
                    self.assertNotEqual(calls[0]["actions"], ("finish",))
                    self.assertEqual(calls[-1]["feedback"]["error"], "invalid_tool_action")
                    self.assertEqual(calls[-1]["actions"], ("finish",))
                self.assertTrue(task.data["report"]["approved"])
                self.assertEqual(task.data["state"], "completed")

    def test_repair_builder_keeps_write_and_check_after_review_latch_and_negative_host_check(self):
        wrong = "def greeting(name):\n    return 'Wrong ' + name\n"
        values = initial_builder(wrong)
        # A negative host check now requires an immediate valid negative finish.
        # This case exercises the subsequent repair builder's permissions.
        values.extend(review_steps(passed=False))
        values.extend([
            {"action": "read_file", "path": PATH},
            {"action": "replace_text_batch", "path": PATH,
             "replacements": [{"old": "'Wrong ' + name", "new": "'Still wrong ' + name"}]},
            {"action": "run_check"},
            {"action": "replace_text_batch", "path": PATH,
             "replacements": [{"old": "'Still wrong ' + name", "new": "'Hallo ' + name"}]},
            {"action": "run_check"}, finish(),
        ])
        values.extend(review_steps(invalid_finish=True))
        values.extend(review_steps(invalid_finish=True))
        client = client_for(values)
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.prepare(store)
                self.assertTrue(build_confirmed(task, client, lambda _: None))
                repair_checks = [call for call in client.calls
                                 if call["role"] == "builder"
                                 and call["feedback"].get("status") == "check_complete"]
                self.assertEqual([call["feedback"]["passed"] for call in repair_checks], [False, True])
                for call in repair_checks:
                    self.assertIn("replace_text_batch", call["actions"])
                    self.assertIn("run_check", call["actions"])
                    self.assertNotEqual(call["actions"], ("finish",))
                self.assertEqual((store.directory / "build" / PATH).read_text(encoding="utf-8"), SOURCE)
                self.assertTrue(task.data["report"]["host_passed"])
                self.assertTrue(task.data["report"]["approved"])


if __name__ == "__main__":
    unittest.main()
