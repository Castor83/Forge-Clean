"""Execution benchmark raw evidence remains complete and immutable at deadlines."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from scripts.benchmark_execution_contract import RecordedChat
from src.forge_runtime.tool_loop import _call_chat_with_deadline, ToolLoopDeadlineExceeded

MESSAGES = (('system', 'test'), ('user', '{"role":"builder"}'))


class ExecutionEvidenceTests(unittest.TestCase):
    def test_deadline_preserves_started_call_and_late_outcomes_do_not_mutate_logs(self):
        for outcome in ('response', 'error'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                started, release, finished = threading.Event(), threading.Event(), threading.Event()
                def client(messages):
                    started.set()
                    if not release.wait(3):
                        raise RuntimeError('test release missing')
                    if outcome == 'error':
                        raise RuntimeError('late failure')
                    return '{"action":"replace_text"}'
                recorder = RecordedChat(client, root)
                def tracked(messages):
                    try:
                        return recorder(messages)
                    finally:
                        finished.set()
                try:
                    with self.assertRaises(ToolLoopDeadlineExceeded) as caught:
                        _call_chat_with_deadline(tracked, MESSAGES, timeout_seconds=0.05,
                            kind='call', role='builder', step=1, limit_seconds=0.05, last_action='none')
                    self.assertTrue(started.is_set())
                    recorder.close(f'{type(caught.exception).__name__}: {caught.exception}')
                    snapshots = {name: (root / name).read_bytes()
                                 for name in ('model_calls.jsonl', 'trace.jsonl')}
                    for raw in snapshots.values():
                        entries = [json.loads(line) for line in raw.splitlines()]
                        self.assertEqual(len(entries), recorder.model_calls)
                        self.assertEqual(entries[0]['call_id'], 1)
                        self.assertFalse(entries[0]['callback_complete'])
                        self.assertIn('ToolLoopDeadlineExceeded', entries[0]['finalization_reason'])
                        self.assertNotIn('response', entries[0])
                    release.set()
                    self.assertTrue(finished.wait(3))
                    recorder.close('second close')
                    for name, raw in snapshots.items():
                        self.assertEqual((root / name).read_bytes(), raw)
                    self.assertEqual(recorder.repair_actions, 0)
                    with self.assertRaisesRegex(ValueError, 'recorder finalized'):
                        recorder(MESSAGES)
                    self.assertEqual(recorder.model_calls, 1)
                finally:
                    release.set()

    def test_completed_success_and_error_are_recorded_once(self):
        with tempfile.TemporaryDirectory() as folder:
            def client(messages):
                if messages[-1][1] == '{"kind":"fail"}':
                    raise RuntimeError('expected failure')
                return '{"action":"replace_text_batch"}'
            root = Path(folder)
            recorder = RecordedChat(client, root)
            recorder(MESSAGES)
            with self.assertRaisesRegex(RuntimeError, 'expected failure'):
                recorder(MESSAGES + (('user', '{"kind":"fail"}'),))
            recorder.close()
            for name in ('model_calls.jsonl', 'trace.jsonl'):
                entries = [json.loads(line) for line in (root / name).read_text().splitlines()]
                self.assertEqual([entry['call_id'] for entry in entries], [1, 2])
                self.assertTrue(all(entry['callback_complete'] for entry in entries))
                self.assertIn('response', entries[0])
                self.assertIn('RuntimeError: expected failure', entries[1]['error'])
            self.assertEqual(recorder.model_calls, 2)
            self.assertEqual(recorder.repair_actions, 1)


if __name__ == '__main__':
    unittest.main()
