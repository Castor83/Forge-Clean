"""Behavioral probes for the confirmed build's isolated, host-bound context."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime import conversation_build, task_runner
from src.forge_runtime.conversation_build import build_confirmed, check_completed
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore, proposal_digest
from src.forge_runtime.project_context import CONTEXT_FILENAME
from src.forge_runtime.project_session import update_project
from src.forge_runtime.task_runner import run_forge_task
from src.forge_runtime.tool_loop import HostCheckFailure


PATH = "agent/hello.py"
SOURCE = "def greeting(name):\n    return (name != '' and 'Hi ' + name + '!') or 'Bitte Namen eingeben.'\n"
SAVED_OBJECTIVE = "OLD_OBJECTIVE: Guten Tag statt Hi ausgeben."
ACTIVE_RULE = "CURRENT_RULE: Keine Imports oder Nebenwirkungen."
PROPOSAL = {
    "task_id": "conversation-build",
    "objective": "greeting(name) gibt Hi <Name>! aus; nur die leere Zeichenkette ergibt "
                 "Bitte Namen eingeben. Leerzeichen unverändert übernehmen. "
                 "Eine importfreie Funktion mit einem return-Ausdruck.",
    "allowed_files": [PATH],
    "checks": [{"kind": "python_function_cases", "path": PATH, "function": "greeting",
                "cases": [{"args": ["Ada"], "expected": "Hi Ada!"},
                          {"args": [""], "expected": "Bitte Namen eingeben."},
                          {"args": [" "], "expected": "Hi  !"},
                          {"args": [" Ada "], "expected": "Hi  Ada !"}]}],
}


def envelopes(messages, kind):
    found = []
    for role, content in messages:
        if role != "user":
            continue
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        if type(value) is dict and value.get("kind") == kind:
            found.append(value)
    return found


def scripted_actions(source=SOURCE, tester_passed=True, reviewer_passed=True):
    actions = [
        {"action": "write_file", "path": PATH, "content": source},
        {"action": "read_file", "path": PATH},
        {"action": "finish", "passed": True, "summary": "Built", "findings": []},
        {"action": "read_file", "path": PATH},
        {"action": "run_check"},
        {"action": "finish", "passed": tester_passed, "summary": "Checked",
         "findings": [] if tester_passed else ["Explicit tester rejection"]},
        {"action": "read_file", "path": PATH},
        {"action": "run_check"},
        {"action": "finish", "passed": reviewer_passed, "summary": "Reviewed",
         "findings": [] if reviewer_passed else ["Explicit reviewer rejection"]},
    ]
    iterator = iter(actions)
    return lambda _: json.dumps(next(iterator))


class ConfiguredClient:
    """Observe the response schema active at each actual model boundary."""

    def __init__(self, respond):
        self.respond = respond
        self.actions = ()
        self.configurations = []
        self.calls = []

    def set_tool_actions(self, actions):
        self.actions = tuple(actions)
        self.configurations.append(self.actions)

    def __call__(self, messages):
        parsed = []
        for role, content in messages:
            if role != "user":
                continue
            try:
                value, _ = json.JSONDecoder().raw_decode(content.lstrip())
            except ValueError:
                continue
            if type(value) is dict:
                parsed.append(value)
        role = next(value["role"] for value in parsed if value.get("role") in
                    {"builder", "tester", "reviewer"})
        feedback = parsed[-1] if parsed else None
        self.calls.append({"role": role, "feedback": feedback, "actions": self.actions})
        return self.respond(messages)


class BuildContextContractTests(unittest.TestCase):
    def prepare(self, store):
        task = ConversationTask(store)
        task.initialize_project("build_contract")
        update_project(task, "rules", "scope", ACTIVE_RULE)
        update_project(task, "decisions", "objective", SAVED_OBJECTIVE)
        update_project(task, "decisions", "acceptance_notes", "OLD_CHECK: Expect Guten Tag.")
        update_project(task, "decisions", "working_notes", "OLD_NOTE: Prefer saved output.")
        discuss(task, "Hi <Name>!; leere Eingabe Bitte Namen eingeben.",
                lambda _: json.dumps({"reply": "Aktueller Hi-Auftrag.", "questions": [],
                                      "task": copy.deepcopy(PROPOSAL)}))
        task.confirm()
        return task

    def test_actual_three_role_build_uses_confirmed_contract_without_saved_objectives(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with TaskStore(directory).exclusive() as store:
                task = self.prepare(store)
                saved = (directory / CONTEXT_FILENAME).read_bytes()
                binding = copy.deepcopy(task.data["context_binding"])
                expected_rules = copy.deepcopy(task.project_data()["rules"])
                reply = scripted_actions()
                calls = []

                def client(messages):
                    calls.append(messages)
                    all_text = "\n".join(content for _, content in messages)
                    self.assertNotIn(SAVED_OBJECTIVE, all_text)
                    self.assertNotIn("OLD_CHECK", all_text)
                    self.assertNotIn("OLD_NOTE", all_text)
                    self.assertEqual(envelopes(messages, "project_context_data"), [])
                    build = envelopes(messages, "build_context_data")
                    self.assertEqual(len(build), 1)
                    self.assertEqual(build[0]["project_binding"], binding)
                    self.assertEqual(build[0]["active_rules"], expected_rules)
                    self.assertEqual(build[0]["confirmed_proposal"], PROPOSAL)
                    self.assertEqual(build[0]["confirmed_proposal_sha256"], proposal_digest(PROPOSAL))
                    for field in ("decisions", "tasks", "next_step", "evidence"):
                        self.assertNotIn(field, build[0])
                    self.assertNotIn(ACTIVE_RULE, messages[0][1])
                    self.assertEqual(sum(role == "system" for role, _ in messages), 1)
                    return reply(messages)

                self.assertTrue(build_confirmed(task, client, lambda _: None))
                self.assertEqual(len(calls), 9)
                self.assertEqual([item["role"] for item in task.data["report"]["results"]],
                                 ["builder", "tester", "reviewer"])
                self.assertTrue(task.data["report"]["host_passed"])
                self.assertEqual((directory / "build" / PATH).read_text(encoding="utf-8"), SOURCE)
                self.assertEqual((directory / CONTEXT_FILENAME).read_bytes(), saved)
            with TaskStore(directory).exclusive() as store:
                task = ConversationTask(store)
                check_completed(task)
                self.assertEqual(task.data["state"], "completed")
                self.assertEqual((directory / CONTEXT_FILENAME).read_bytes(), saved)

    def mutate(self, task, mutation):
        if mutation == "proposal":
            task.data["proposal"]["objective"] = "Replace the confirmed Hi objective."
        elif mutation == "case":
            task.data["proposal"]["checks"][0]["cases"][0]["expected"] = "Guten Tag Ada"
        elif mutation == "proposal_confirmation":
            task.data["confirmed_proposal_sha256"] = "0" * 64
        elif mutation == "context_confirmation":
            task.data["confirmed_context_sha256"] = "0" * 64
        elif mutation == "state":
            task.data["state"] = "ready"
        elif mutation == "binding":
            task.data["context_binding"]["sha256"] = "0" * 64
        elif mutation == "omitted_context":
            path = task.store.directory / CONTEXT_FILENAME
            context = json.loads(path.read_text(encoding="utf-8"))
            context["decisions"][0]["value"] = "Changed omitted saved objective."
            path.write_text(json.dumps(context), encoding="utf-8")

    def test_context_and_confirmation_mutations_block_before_and_after_model_contact(self):
        mutations = ("proposal", "case", "proposal_confirmation", "context_confirmation",
                     "state", "binding", "omitted_context")
        for timing in ("before", "after"):
            for mutation in mutations:
                with self.subTest(timing=timing, mutation=mutation), tempfile.TemporaryDirectory() as folder:
                    with TaskStore(Path(folder)).exclusive() as store:
                        task = self.prepare(store)
                        proposal = copy.deepcopy(task.data["proposal"])
                        task.change(state="running", event="Isolated build guard probe.")
                        calls = []

                        def client(messages):
                            calls.append(messages)
                            if timing == "after":
                                self.mutate(task, mutation)
                            return "model reply that must not be accepted"

                        guard = task.guarded_chat(client, build_proposal=proposal)
                        if timing == "before":
                            self.mutate(task, mutation)
                        with self.assertRaises(ValueError):
                            guard((("system", "Fixed host instructions"), ("user", "Build current task")))
                        self.assertEqual(len(calls), 0 if timing == "before" else 1)
                        self.assertFalse((store.directory / "build" / PATH).exists())

    def test_build_guard_refuses_changed_snapshot_before_contact(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.prepare(store)
                task.change(state="running", event="Isolated build guard probe.")
                wrong = copy.deepcopy(task.data["proposal"])
                wrong["objective"] = "Different unconfirmed task."
                with self.assertRaises(ValueError):
                    guard = task.guarded_chat(lambda _: self.fail("unconfirmed snapshot reached model"),
                                              build_proposal=wrong)
                    guard((("system", "Fixed host instructions"), ("user", "Build current task")))

    def test_positive_host_check_does_not_overrule_negative_tester_or_reviewer(self):
        for rejected_role in ("tester", "reviewer"):
            with self.subTest(rejected_role=rejected_role), tempfile.TemporaryDirectory() as folder:
                with TaskStore(Path(folder)).exclusive() as store:
                    task = self.prepare(store)
                    client = scripted_actions(tester_passed=rejected_role != "tester",
                                              reviewer_passed=rejected_role != "reviewer")
                    self.assertFalse(build_confirmed(task, client, lambda _: None))
                    self.assertEqual(task.data["state"], "failed")
                    self.assertTrue(task.data["report"]["host_passed"])
                    self.assertFalse(task.data["report"]["pipeline_approved"])
                    self.assertFalse(task.data["report"]["approved"])
                    self.assertEqual(task.data["report"]["results"][-1]["role"], rejected_role)

    def test_wrong_saved_output_cannot_receive_host_success_even_if_tester_claims_pass(self):
        wrong_source = "def greeting(name):\n    return 'Guten Tag ' + name\n"

        def no_repair(*args, **kwargs):
            kwargs["max_repair_attempts"] = 0
            return run_forge_task(*args, **kwargs)

        for tester_claim in (False, True):
            with self.subTest(tester_claim=tester_claim), tempfile.TemporaryDirectory() as folder:
                with TaskStore(Path(folder)).exclusive() as store:
                    task = self.prepare(store)
                    client = scripted_actions(source=wrong_source, tester_passed=tester_claim)
                    with patch.object(conversation_build, "run_forge_task", side_effect=no_repair):
                        if tester_claim:
                            with self.assertRaises(HostCheckFailure):
                                build_confirmed(task, client, lambda _: None)
                        else:
                            self.assertFalse(build_confirmed(task, client, lambda _: None))
                            self.assertFalse(task.data["report"]["host_passed"])
                    self.assertEqual(task.data["state"], "failed")
                    self.assertFalse(task.data["report"].get("approved", False))
                    self.assertEqual((store.directory / "build" / PATH).read_text(encoding="utf-8"),
                                     wrong_source)

    def test_successful_review_roles_receive_only_finish_after_completed_check(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.prepare(store)
                client = ConfiguredClient(scripted_actions())
                self.assertTrue(build_confirmed(task, client, lambda _: None))
                completed = [call for call in client.calls
                             if call["feedback"].get("status") == "check_complete"]
                self.assertEqual([call["role"] for call in completed], ["tester", "reviewer"])
                self.assertTrue(all(call["actions"] == ("finish",) for call in completed))
                self.assertTrue(all(call["feedback"]["passed"] for call in completed))
                self.assertEqual(client.configurations.count(("finish",)), 2)
                self.assertTrue(task.data["report"]["approved"])

    def test_finish_only_schema_still_allows_negative_role_decision(self):
        for rejected in ("tester", "reviewer"):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as folder:
                with TaskStore(Path(folder)).exclusive() as store:
                    task = self.prepare(store)
                    client = ConfiguredClient(scripted_actions(tester_passed=rejected != "tester",
                                                               reviewer_passed=rejected != "reviewer"))
                    self.assertFalse(build_confirmed(task, client, lambda _: None))
                    self.assertEqual(client.calls[-1]["role"], rejected)
                    self.assertEqual(client.calls[-1]["actions"], ("finish",))
                    self.assertTrue(client.calls[-1]["feedback"]["passed"])
                    self.assertFalse(task.data["report"]["results"][-1]["passed"])
                    self.assertFalse(task.data["report"]["approved"])

    def test_repeated_role_check_is_rejected_before_another_host_check_runs(self):
        original_check = task_runner._StaticCheckRunner.__call__
        for repeated in ("tester", "reviewer"):
            with self.subTest(repeated=repeated), tempfile.TemporaryDirectory() as folder:
                with TaskStore(Path(folder)).exclusive() as store:
                    task = self.prepare(store)
                    respond = scripted_actions()
                    client = None

                    def repeat_check(messages):
                        current = client.calls[-1]
                        if (current["role"] == repeated
                                and current["feedback"].get("status") == "check_complete"):
                            return json.dumps({"action": "run_check"})
                        return respond(messages)

                    client = ConfiguredClient(repeat_check)
                    with patch.object(task_runner._StaticCheckRunner, "__call__", autospec=True,
                                      side_effect=original_check) as check:
                        with self.assertRaises(ValueError):
                            build_confirmed(task, client, lambda _: None)
                    self.assertEqual(check.call_count, 1 if repeated == "tester" else 2)
                    self.assertEqual(client.calls[-1]["role"], repeated)
                    self.assertEqual(client.calls[-1]["actions"], ("finish",))
                    self.assertEqual(task.data["state"], "failed")
                    self.assertFalse(task.data["report"].get("approved", False))

    def test_repair_builder_can_replace_after_negative_check_then_complete_pipeline(self):
        wrong = "def greeting(name):\n    return 'Guten Tag ' + name\n"
        actions = [
            {"action": "write_file", "path": PATH, "content": wrong},
            {"action": "read_file", "path": PATH},
            {"action": "finish", "passed": True, "summary": "Initial build", "findings": []},
            {"action": "read_file", "path": PATH},
            {"action": "run_check"},
            {"action": "finish", "passed": False, "summary": "Mismatch", "findings": ["Wrong output"]},
            {"action": "read_file", "path": PATH},
            {"action": "replace_text_batch", "path": PATH,
             "replacements": [{"old": "'Guten Tag ' + name", "new": "'Still wrong ' + name"}]},
            {"action": "run_check"},
            {"action": "replace_text_batch", "path": PATH,
             "replacements": [{"old": "'Still wrong ' + name",
                               "new": "(name != '' and 'Hi ' + name + '!') or 'Bitte Namen eingeben.'"}]},
            {"action": "run_check"},
            {"action": "finish", "passed": True, "summary": "Repaired", "findings": []},
            {"action": "read_file", "path": PATH},
            {"action": "run_check"},
            {"action": "finish", "passed": True, "summary": "Checked", "findings": []},
            {"action": "read_file", "path": PATH},
            {"action": "run_check"},
            {"action": "finish", "passed": True, "summary": "Reviewed", "findings": []},
        ]
        iterator = iter(actions)
        client = ConfiguredClient(lambda _: json.dumps(next(iterator)))
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = self.prepare(store)
                self.assertTrue(build_confirmed(task, client, lambda _: None))
                repair_feedback = [call for call in client.calls
                                   if call["role"] == "builder"
                                   and call["feedback"].get("status") == "check_complete"]
                self.assertEqual([call["feedback"]["passed"] for call in repair_feedback], [False, True])
                for call in repair_feedback:
                    self.assertIn("replace_text_batch", call["actions"])
                    self.assertIn("run_check", call["actions"])
                    self.assertNotEqual(call["actions"], ("finish",))
                self.assertEqual((store.directory / "build" / PATH).read_text(encoding="utf-8"), SOURCE)
                self.assertTrue(task.data["report"]["host_passed"])
                self.assertTrue(task.data["report"]["approved"])


if __name__ == "__main__":
    unittest.main()
