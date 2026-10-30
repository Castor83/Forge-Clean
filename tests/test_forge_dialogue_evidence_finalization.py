"""Frozen deadline evidence and a decodable prompt example."""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_dialogue_quality as quality
from src.forge_runtime.conversation_protocol import SYSTEM_PROMPT, parse_conversation_response
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from src.forge_runtime.tool_loop import _call_chat_with_deadline, ToolLoopDeadlineExceeded


class EvidenceFinalizationTests(unittest.TestCase):
    def test_prompt_example_decodes_and_preserves_exact_dot_cases(self):
        start = SYSTEM_PROMPT.index('{"reply":"Ich schlage einen Textbaustein')
        example, _ = json.JSONDecoder().raw_decode(SYSTEM_PROMPT[start:])
        parsed = parse_conversation_response(json.dumps(example, ensure_ascii=False))
        self.assertTrue(parsed['reply'].startswith('Ich schlage '))
        self.assertNotIn('\n', parsed['reply'])
        self.assertEqual(parsed['questions'], [])
        self.assertEqual(parsed['task']['checks'][0]['cases'], [
            {'args': ['Test'], 'expected': 'Test.'},
            {'args': [''], 'expected': '.'},
            {'args': [' '], 'expected': ' .'}])

    def test_late_success_and_error_cannot_change_bound_transcript(self):
        for outcome in ('response', 'error'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                started = threading.Event()
                release = threading.Event()
                finished = threading.Event()
                def client(messages):
                    started.set()
                    if not release.wait(3):
                        raise RuntimeError('test release missing')
                    if outcome == 'error':
                        raise RuntimeError('late failure')
                    return 'late raw response'
                def deadline(task, message, callback, **kwargs):
                    # Use the real host deadline with a deliberately blocked callback.
                    def tracked(messages):
                        try:
                            return callback(messages)
                        finally:
                            finished.set()
                    return _call_chat_with_deadline(tracked, (('user', message),),
                        timeout_seconds=0.05, kind='call', limit_seconds=0.05,
                        role='conversation', step=1, last_action='clarifying')
                with TaskStore(directory).exclusive() as store:
                    task = ConversationTask(store)
                    report = {'host_commands': [], 'turns': []}
                    quality.setup(task, 'typo_request', report)
                    with patch.object(quality, 'discuss', side_effect=deadline), \
                         patch.object(quality.paths, 'assert_actual_envelope'), \
                         self.assertRaises(ToolLoopDeadlineExceeded):
                        quality.turn(task, 'typo_request', 'Textregel', client, directory, report, 'current_message')
                    self.assertTrue(started.is_set())
                    raw = (directory / 'model_dialogue.jsonl').read_bytes()
                    bound = hashlib.sha256(raw).hexdigest()
                    entry = json.loads(raw)
                    self.assertFalse(entry['callback_complete'])
                    self.assertFalse(report['turns'][0]['raw_callbacks_complete'])
                    self.assertEqual(report['turns'][0]['raw_turn_sha256'], bound)
                    release.set()
                    self.assertTrue(finished.wait(3))
                    self.assertEqual((directory / 'model_dialogue.jsonl').read_bytes(), raw)
                    late = json.loads((directory / 'model_dialogue_late.jsonl').read_text(encoding='utf-8'))
                    self.assertEqual(late['finalized_turn_sha256'], bound)
                    self.assertTrue(late['callback_complete'])
                    self.assertIn(outcome, late)
