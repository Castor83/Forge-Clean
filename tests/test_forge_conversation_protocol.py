import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_protocol import discuss, parse_conversation_response
from src.forge_runtime.conversation_store import ConversationTask, TaskStore


def proposal():
    return {"task_id": "conversation-build", "objective": "Begrüße den Namen.",
            "allowed_files": ["agent/hello.py"], "checks": [{
                "kind": "python_function_cases", "path": "agent/hello.py", "function": "greeting",
                "cases": [{"args": ["Ada"], "expected": "Hallo Ada"}, {"args": [""], "expected": "Hallo "}]}]}


def answer(task=None, questions=None):
    return json.dumps({"reply": "Ich baue eine Begrüßung." if task else "Welche Begrüßung möchtest du?",
                       "questions": questions if questions is not None else [], "task": task})


class ConversationProtocolTests(unittest.TestCase):
    def test_clarification_uses_context_and_persists_proposal(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                discuss(task, "Bau mir einen Begrüsser", lambda _: answer(questions=["Welche Begrüßung?"]))
                self.assertEqual(task.data["state"], "clarifying")
                with self.assertRaises(ValueError):
                    task.confirm()
                seen = []
                def chat(messages):
                    seen.extend(messages)
                    return answer(proposal())
                discuss(task, "Hallo und der Name", chat)
                self.assertEqual([m[0] for m in seen], ["system", "user", "assistant", "user"])
                self.assertIn("Begrüsser", seen[1][1])
                task.confirm()
                self.assertEqual(task.data["state"], "ready")

    def test_new_message_revokes_readiness_even_if_model_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                discuss(task, "Begrüße", lambda _: answer(proposal()))
                task.confirm()
                with self.assertRaises(ValueError):
                    discuss(task, "Andere Ausgabe bitte", lambda _: "invalid")
                self.assertEqual(task.data["state"], "clarifying")
                self.assertIsNone(task.data["proposal"])
                self.assertEqual(task.data["messages"][-1]["content"], "Andere Ausgabe bitte")

    def test_rejects_scope_escape_weak_checks_and_fake_permission(self):
        variations = []
        for path in ("../x.py", "C:/x.py", "agent/CON.py", "agent/x.txt"):
            value = proposal()
            value["allowed_files"] = [path]
            value["checks"][0]["path"] = path
            variations.append(value)
        weak = proposal()
        weak["checks"] = [{"kind": "file_exists", "path": "agent/hello.py"}]
        variations.append(weak)
        duplicate = proposal()
        duplicate["checks"][0]["cases"][1] = copy.deepcopy(duplicate["checks"][0]["cases"][0])
        variations.append(duplicate)
        for value in variations:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_conversation_response(answer(value))
        with self.assertRaises(ValueError):
            parse_conversation_response(json.dumps({"reply": "ok", "questions": [], "task": proposal(), "approved": True}))

    def test_questions_and_proposal_cannot_coexist(self):
        with self.assertRaises(ValueError):
            parse_conversation_response(answer(proposal(), ["Noch offen?"]))
        with self.assertRaises(ValueError):
            parse_conversation_response(answer())

    def test_closed_task_rejects_more_messages_without_model_call(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                task.cancel()
                with self.assertRaises(ValueError):
                    discuss(task, "neuer Auftrag", lambda _: self.fail("Model called"))
