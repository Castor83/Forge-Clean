import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.forge_runtime.contracts import SubagentResult, SubagentTask
from src.forge_runtime.tool_loop import HostCheckFailure, run_tool_loop
from src.forge_runtime.workspace_tools import WorkspaceFileTools
from src.forge_runtime import run_forge_task
from test_forge_repair_loop import RecordingChat, finish, repair_spec


class TestNegativeCheckFinish(unittest.TestCase):
    def run_role(self, role, responses, check, max_steps=12):
        task = SubagentTask('negative-finish', 'Check file', ('result.txt',))
        previous = tuple(SubagentResult(r, True, 'Done', ()) for r in
                         (('builder',) if role == 'tester' else ('builder', 'tester')))
        with tempfile.TemporaryDirectory() as directory:
            workspace = WorkspaceFileTools(Path(directory), task)
            chat = RecordingChat(responses)
            self.last_chat = chat
            with patch.object(WorkspaceFileTools, 'read_text', wraps=workspace.read_text) as read:
                self.last_read = read
                result = run_tool_loop(role, task, previous, workspace, chat,
                                       check_runner=check, max_steps=max_steps)
                return result, chat, read.call_count

    def test_invalid_followup_stops_without_another_tool_or_model_call(self):
        actions = ('not json', '{"action":"unknown"}',
                   '{"action":"read_file","path":"result.txt"}',
                   '{"action":"run_check"}', finish('false success'))
        for role in ('tester', 'reviewer'):
            for action in actions:
                with self.subTest(role=role, action=action):
                    checks = []
                    def check():
                        checks.append(1)
                        return False, 'REAL HOST FAILURE'
                    with self.assertRaises(HostCheckFailure) as caught:
                        self.run_role(role, ['{"action":"run_check"}', action,
                                            finish('late rejection', False)], check)
                    self.assertEqual(caught.exception.detail, 'REAL HOST FAILURE')
                    self.assertEqual(len(checks), 1)
                    self.assertEqual(len(self.last_chat.calls), 2)
                    self.assertEqual(self.last_read.call_count, 0)

    def test_last_step_preserves_host_failure(self):
        for role in ('tester', 'reviewer'):
            with self.subTest(role=role), self.assertRaises(HostCheckFailure) as caught:
                self.run_role(role, ['{"action":"run_check"}'],
                              lambda: (False, 'LAST SLOT'), max_steps=1)
            self.assertEqual(caught.exception.detail, 'LAST SLOT')

    def test_real_negative_finish_is_preserved(self):
        for role in ('tester', 'reviewer'):
            result, chat, reads = self.run_role(
                role, ['{"action":"run_check"}', json.dumps(dict(
                    action='finish', passed=False, summary='actual rejection',
                    findings=['specific finding']))], lambda: (False, 'failure'))
            self.assertEqual(result, SubagentResult(role, False, 'actual rejection',
                                                   ('specific finding',)))
            self.assertEqual(len(chat.calls), 2)
            self.assertEqual(reads, 0)

    def test_positive_check_and_unchecked_rejection_remain_valid(self):
        for role in ('tester', 'reviewer'):
            result, _, _ = self.run_role(role, ['{"action":"run_check"}', finish('ok')],
                                         lambda: (True, 'ok'))
            self.assertTrue(result.passed)
            result, _, _ = self.run_role(role, [finish('unchecked rejection', False)],
                                         lambda: (False, 'not called'))
            self.assertFalse(result.passed)

    def test_real_task_repair_budget_zero_and_one(self):
        for budget in (0, 1):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                chat = RecordingChat([
                    json.dumps(dict(action='write_file', path='result.txt', content='DRAFT')),
                    finish('built'), '{"action":"run_check"}',
                    '{"action":"read_file","path":"result.txt"}',
                    json.dumps(dict(action='replace_text_batch', path='result.txt',
                                    replacements=[dict(old='DRAFT', new='FINAL')])),
                    '{"action":"run_check"}', finish('repaired'),
                    '{"action":"run_check"}', finish('tested'),
                    '{"action":"run_check"}', finish('reviewed')])
                root = Path(directory)
                if budget == 0:
                    with self.assertRaises(HostCheckFailure):
                        run_forge_task(root, repair_spec(), chat, max_repair_attempts=budget)
                    self.assertEqual(len(chat.calls), 4)
                    self.assertEqual((root / 'result.txt').read_text(), 'DRAFT')
                else:
                    result = run_forge_task(root, repair_spec(), chat, max_repair_attempts=budget)
                    self.assertTrue(result.approved)
                    self.assertEqual((root / 'result.txt').read_text(), 'FINAL')
                    feedback = json.loads(chat.calls[4][2][1])
                    self.assertTrue(feedback['trusted'])
                    self.assertIn('required text is missing: FINAL', feedback['output'])
