"""Acceptance never retries malformed answers or releases another model."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_dialogue_quality as quality
from src.forge_runtime.conversation_protocol import discuss
from src.forge_runtime.conversation_store import ConversationTask, TaskStore
from tests.test_forge_dialogue_quality_acceptance import reviewed_fixture


class SingleCallAcceptanceTests(unittest.TestCase):
    def test_no_correction_mode_keeps_invalid_raw_call_and_revoked_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                calls = []
                def invalid(messages):
                    calls.append(messages)
                    return 'not JSON'
                with self.assertRaisesRegex(ValueError, 'Hostkorrektur'):
                    discuss(task, 'Ich brauche eine Textregel.', invalid, allow_correction=False)
                self.assertEqual(len(calls), 1)
                self.assertEqual(task.data['state'], 'clarifying')
                self.assertIsNone(task.data['proposal'])
                self.assertFalse(task.is_confirmed())
                self.assertEqual(task.data['messages'][-1]['content'], 'Ich brauche eine Textregel.')

    def test_non_boolean_correction_option_calls_no_model(self):
        with tempfile.TemporaryDirectory() as folder:
            with TaskStore(Path(folder)).exclusive() as store:
                task = ConversationTask(store)
                calls = []
                for value in (None, 0, 1, 'False'):
                    with self.subTest(value=value), self.assertRaises(ValueError):
                        discuss(task, 'Textregel', lambda messages: calls.append(messages), allow_correction=value)
                self.assertEqual(calls, [])

    def test_model_binding_with_complete_valid_report_and_raw_evidence(self):
        for model in ('forge-30b', 'another-model', None):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                report, review = reviewed_fixture()
                report['model'] = model
                for case in report['scenarios']:
                    child = directory / case['name']
                    child.mkdir()
                    case['process_id'] = 101
                    records = []
                    for turn in case['turns']:
                        records.append({'process_id': 102 if turn['stage'] == 'restart_current_message' else 101,
                                        'stage': turn['stage'], 'messages': [['user', turn['message']]],
                                        'confirmation_revoked_before_call': True,
                                        'response': json.dumps(turn['response'])})
                    raw = ''.join(json.dumps(record) + '\n' for record in records).encode('utf-8')
                    (child / 'model_dialogue.jsonl').write_bytes(raw)
                    case['transcript_sha256'] = hashlib.sha256(raw).hexdigest()
                    case['evidence_sha256'] = quality.evidence_digest(case)
                    quality.write_new(child, 'scenario_report.json', case)
                    if case['name'] == 'restart_current_decision':
                        quality.write_new(child, 'restart_prepare.json', {
                            'source_sha256': report['source_sha256'], 'process_id': 101})
                        quality.write_new(child, 'restart_resume_report.json', {
                            'process_id': 102, 'automatic_passed': True, 'turns': case['turns'][1:]})
                        quality.write_new(child, 'restart_process_output.json', {'returncode': 0})
                for case, checked in zip(report['scenarios'], review['scenarios']):
                    checked['evidence_sha256'] = case['evidence_sha256']
                quality.write_new(directory, 'dialogue_quality_run_report.json', report)
                review['report_sha256'] = hashlib.sha256((directory / 'dialogue_quality_run_report.json').read_bytes()).hexdigest()
                quality.write_new(directory, 'review.json', review)
                with patch.object(quality, 'ROOT', directory), patch.object(quality, 'source_hashes', return_value=report['source_sha256']):
                    if model == 'forge-30b':
                        quality.apply_review(directory, directory / 'review.json')
                    else:
                        with self.assertRaisesRegex(ValueError, 'Modell forge-30b'):
                            quality.apply_review(directory, directory / 'review.json')
                self.assertEqual((directory / 'dialogue_quality_acceptance_report.json').exists(), model == 'forge-30b')
