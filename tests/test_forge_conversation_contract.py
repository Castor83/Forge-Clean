import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.conversation_build import build_confirmed
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.contracts import PipelineResult, SubagentResult
from tests.test_forge_conversation_build import ready


class ConversationContractTests(unittest.TestCase):
    def test_builder_receives_exact_confirmed_cases_and_host_constraints(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder))
            with store.exclusive():
                task = ConversationTask(store)
                ready(task)
                result = PipelineResult((SubagentResult("builder", False, "failure", ()),))
                with patch("src.forge_runtime.conversation_build.run_forge_task", return_value=result) as runner:
                    build_confirmed(task, lambda _: None, lambda _: None)
                objective = runner.call_args.args[1].task.objective
                self.assertIn('"expected": "Hallo Ada"', objective)
                self.assertIn('"expected": "Hallo "', objective)
                self.assertIn("No if statements or conditional expressions", objective)
                self.assertEqual(task.data["proposal"]["objective"], "Begrüße den Namen.")
                self.assertFalse(task.data["report"]["approved"])
