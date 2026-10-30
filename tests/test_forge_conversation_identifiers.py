import ast
import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_protocol import discuss, parse_proposal_task
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationIdentifierTests(unittest.TestCase):
    def test_python_normalized_name_is_rejected_before_build(self):
        for name in ("ｇｒｅｅｔｉｎｇ", "K", "gru\u0308sse"):
            value = proposal()
            value["checks"][0]["function"] = name
            # Python changes the identifier; the exact proposed name cannot be checked.
            tree = ast.parse("def " + name + "(value):\n    return value\n")
            self.assertNotEqual(tree.body[0].name, name)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Schreibweise"):
                parse_proposal_task(value)

    def test_canonical_german_function_name_builds_and_passes_actual_host_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                value = proposal()
                value["checks"][0]["function"] = "begrüße"
                discuss(task, "Begrüßung mit Name", lambda _: answer(value))
                task.confirm()
                steps = iter([
                    {"action": "write_file", "path": "agent/hello.py", "content": "def begrüße(name):\n    return 'Hallo ' + name\n"},
                    {"action": "finish", "passed": True, "summary": "gebaut", "findings": []},
                    {"action": "run_check"},
                    {"action": "finish", "passed": True, "summary": "geprüft", "findings": []},
                    {"action": "run_check"},
                    {"action": "finish", "passed": True, "summary": "abgenommen", "findings": []}])
                self.assertTrue(build_confirmed(task, lambda _: json.dumps(next(steps)), lambda _: None))
                self.assertEqual(task.data["state"], "completed")
                self.assertTrue(task.data["report"]["host_passed"])
