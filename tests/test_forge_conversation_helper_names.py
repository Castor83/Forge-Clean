import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.conversation_protocol import discuss, parse_proposal_task
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_conversation_protocol import answer, proposal


class ConversationHelperNamesTests(unittest.TestCase):
    def test_function_names_cannot_override_host_helpers(self):
        for name in ("bool", "isinstance", "str"):
            value = proposal()
            value["checks"][0]["function"] = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Hilfsfunktion"):
                parse_proposal_task(value)

    def test_helper_name_gets_one_correction_before_user_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                value = proposal()
                value["checks"][0]["function"] = "bool"
                responses = iter([answer(value), answer(proposal())])
                seen = []

                def chat(messages):
                    seen.append(messages)
                    return next(responses)

                discuss(task, "Baue einen kleinen Helfer", chat)
                self.assertEqual(len(seen), 2)
                self.assertIn("Hilfsfunktion", json.loads(seen[1][-1][1])["error"])
                self.assertEqual(task.data["proposal"], proposal())
                self.assertFalse(task.is_confirmed())
