"""Offline behavior checks for the one-build measurement and its evidence gates."""
import copy
import http.client
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.request import Request

from scripts import benchmark_retry_build as benchmark


REPOSITORY = Path(__file__).resolve().parents[1]


class BytesSocket:
    def __init__(self, raw):
        self.raw = raw

    def makefile(self, mode):
        return io.BytesIO(self.raw)


def response(body, declared=None):
    length = len(body) if declared is None else declared
    wire = (f'HTTP/1.1 200 OK\r\nContent-Length: {length}\r\n'
            'Content-Type: application/json\r\n\r\n').encode() + body
    result = http.client.HTTPResponse(BytesSocket(wire))
    result.begin()
    return result


def finish(summary):
    return {'action': 'finish', 'passed': True, 'summary': summary, 'findings': []}


class TestForgeRetryBuildGuards(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix='retry-guard-', dir=REPOSITORY / 'workspaces')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = json.loads((REPOSITORY / benchmark.TASK).read_text(encoding='utf-8'))
        for name in (benchmark.TASK, 'AGENTS.md', 'FORGE_NEXT_BUILD_PROPOSAL.md',
                     'scripts/benchmark_retry_build.py', 'scripts/benchmark_execution_contract.py',
                     'scripts/verify_dialogue_host_gate.py', 'tests/test_forge_retry_build_guards.py',
                     'src/host_probe.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.task) if name == benchmark.TASK else '# fixed source\n',
                            encoding='utf-8')
        root_patch = patch.object(benchmark, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.freeze = self.root / 'freeze.json'
        self.pin = benchmark.prepare(self.freeze)

    def request(self, task=None, model=None):
        task = self.task if task is None else task
        handoff = {key: task[key] for key in ('task_id', 'objective', 'allowed_files')}
        handoff['role'] = 'builder'
        handoff['completed_roles'] = []
        payload = {'model': benchmark.CONFIG['model'] if model is None else model,
                   'temperature': 0, 'stream': False,
                   'messages': [{'role': 'system', 'content': 'offline'},
                                {'role': 'user', 'content': json.dumps(handoff)}]}
        return Request(benchmark.CONFIG['endpoint'], data=json.dumps(payload).encode(),
                       headers={'Content-Type': 'application/json'}, method='POST')

    def test_binding_is_exact_and_prepare_refuses_to_overwrite(self):
        self.assertEqual(benchmark.guard(self.freeze, self.pin), benchmark.current_state())
        original = self.freeze.read_bytes()
        with self.assertRaises(FileExistsError):
            benchmark.prepare(self.freeze)
        self.assertEqual(self.freeze.read_bytes(), original)
        self.assertEqual(len(self.task['checks'][-1]['cases']), 27)
        self.assertEqual(sum(case['expected'] is True for case in self.task['checks'][-1]['cases']), 3)
        self.assertTrue(all(type(case['expected']) is bool for case in self.task['checks'][-1]['cases']))

    def test_drift_is_rejected_before_any_transport(self):
        initial_freeze = self.freeze.read_bytes()
        initial_task = (self.root / benchmark.TASK).read_bytes()
        probe = self.root / 'src/host_probe.py'
        added = self.root / 'src/added.py'
        for change in ('freeze', 'source', 'inventory', 'configuration', 'cases'):
            with self.subTest(change=change):
                self.freeze.write_bytes(initial_freeze)
                (self.root / benchmark.TASK).write_bytes(initial_task)
                probe.write_text('# fixed source\n', encoding='utf-8')
                added.unlink(missing_ok=True)
                changed_configuration = dict(benchmark.CONFIG)
                if change == 'freeze':
                    self.freeze.write_bytes(initial_freeze + b' ')
                elif change == 'source':
                    probe.write_text('# modified source\n', encoding='utf-8')
                elif change == 'inventory':
                    added.write_text('# unexpected source\n', encoding='utf-8')
                elif change == 'configuration':
                    changed_configuration['max_repair_attempts'] = 2
                else:
                    altered = copy.deepcopy(self.task)
                    # False compares equal to integer 0 in Python: source bytes must bind the type.
                    altered['checks'][-1]['cases'][15]['args'][1] = False
                    (self.root / benchmark.TASK).write_text(json.dumps(altered), encoding='utf-8')
                transport = benchmark.GuardedTransport(self.root, self.freeze, self.pin)
                with patch.object(benchmark, 'CONFIG', changed_configuration), \
                        patch.object(benchmark.http.client, 'HTTPConnection') as connection:
                    with self.assertRaises(ValueError):
                        transport(self.request(), benchmark.CONFIG['max_call_seconds'])
                    connection.assert_not_called()
                self.assertEqual(transport.attempts, 0)
                self.assertTrue(transport.failed)
                self.assertFalse((self.root / 'request-001.json').exists())

    def test_outgoing_task_model_endpoint_and_timeout_are_bound(self):
        self.assertEqual(benchmark.validate_request(self.request(), 300, self.task), self.request().data)
        altered = copy.deepcopy(self.task)
        altered['objective'] += '\nChanged requirement.'
        requests = [self.request(altered), self.request(model='different-model'),
                    Request('http://127.0.0.1:1235/v1/chat/completions',
                            data=self.request().data, headers={'Content-Type': 'application/json'},
                            method='POST')]
        for request in requests:
            with self.subTest(url=request.full_url, body=request.data):
                with self.assertRaises(ValueError):
                    benchmark.validate_request(request, 300, self.task)
        with self.assertRaises(ValueError):
            benchmark.validate_request(self.request(), 299, self.task)

    def test_incomplete_http_body_keeps_raw_bytes_and_stops_transport(self):
        body = b'{"choices": []}'  # Valid JSON is insufficient if HTTP declared more bytes.
        raw, metadata = benchmark.receive_response(response(body), self.root / 'complete.json')
        self.assertEqual(raw, body)
        self.assertTrue(metadata['body_complete'])
        transport = benchmark.GuardedTransport(self.root, self.freeze, self.pin)
        with patch.object(benchmark.http.client, 'HTTPConnection') as connection:
            connection.return_value.getresponse.return_value = response(body, len(body) + 7)
            with self.assertRaisesRegex(ValueError, 'incomplete response'):
                transport(self.request(), 300)
            with self.assertRaisesRegex(ValueError, 'earlier failure'):
                transport(self.request(), 300)
            self.assertEqual(connection.call_count, 1)
            connection.return_value.close.assert_called_once()
        self.assertEqual((self.root / 'http-response-001.json').read_bytes(), body)
        metadata = json.loads((self.root / 'http-metadata-001.json').read_text())
        self.assertFalse(metadata['body_complete'])
        self.assertEqual(metadata['body_sha256'], benchmark.digest(body))
        self.assertEqual(transport.attempts, 1)
        self.assertTrue(transport.failed)

    def test_repair_requires_the_bound_host_feedback_before_transport(self):
        negative = 'check 3/python_function_cases/agent/retry_decision.py failed: expected True'
        payload = json.loads(self.request().data)
        handoff = json.loads(payload['messages'][1]['content'])
        handoff.pop('completed_roles')
        handoff['objective'] = benchmark.REPAIR_OBJECTIVE
        payload['messages'][1]['content'] = json.dumps(handoff)
        payload['messages'].append({'role': 'user', 'content': json.dumps({
            'kind': 'host_check_feedback', 'trusted': True, 'output': negative})})
        for change in ('unbound', 'different-finding', 'untrusted', 'tester'):
            with self.subTest(change=change):
                altered = copy.deepcopy(payload)
                bound = negative
                if change == 'unbound':
                    bound = None
                elif change == 'tester':
                    other_role = dict(handoff, role='tester')
                    altered['messages'][1]['content'] = json.dumps(other_role)
                else:
                    feedback = json.loads(altered['messages'][2]['content'])
                    if change == 'different-finding':
                        feedback['output'] += ' changed'
                    else:
                        feedback['trusted'] = False
                    altered['messages'][2]['content'] = json.dumps(feedback)
                request = Request(benchmark.CONFIG['endpoint'], data=json.dumps(altered).encode(),
                                  headers={'Content-Type': 'application/json'}, method='POST')
                transport = benchmark.GuardedTransport(self.root, self.freeze, self.pin)
                transport.repair_feedback = bound
                with patch.object(benchmark.http.client, 'HTTPConnection') as connection:
                    with self.assertRaisesRegex(ValueError, 'bound negative host feedback'):
                        transport(request, 300)
                    connection.assert_not_called()
                self.assertEqual(transport.attempts, 0)
        request = Request(benchmark.CONFIG['endpoint'], data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        self.assertEqual(benchmark.validate_request(request, 300, self.task, negative), request.data)

    def test_existing_run_directory_is_preserved_before_client_or_runtime(self):
        directory = self.root / 'existing-run'
        directory.mkdir()
        (directory / 'evidence.txt').write_bytes(b'keep earlier evidence')
        with patch.object(benchmark, 'LMStudioChatClient') as client, \
                patch.object(benchmark, 'runtime_snapshot') as runtime:
            with self.assertRaises(FileExistsError):
                benchmark.run_build(directory, self.freeze, self.pin)
            client.assert_not_called()
            runtime.assert_not_called()
        self.assertEqual((directory / 'evidence.txt').read_bytes(), b'keep earlier evidence')
        self.assertEqual([path.name for path in directory.iterdir()], ['evidence.txt'])

    def test_real_host_repair_preserves_both_builder_artifacts(self):
        initial = 'def should_retry(error_kind, already_retried):\n    return False\n'
        repaired = ('def should_retry(error_kind, already_retried):\n'
                    '    return isinstance(error_kind, str) and error_kind.strip() == "temporary" '
                    'and already_retried is False\n')
        actions = iter([
            {'action': 'write_file', 'path': benchmark.OUTPUT, 'content': initial},
            finish('synthetic initial build'),
            {'action': 'run_check'},
            finish('synthetic false success triggers HostCheckFailure'),
            {'action': 'read_file', 'path': benchmark.OUTPUT},
            {'action': 'replace_text_batch', 'path': benchmark.OUTPUT,
             'replacements': [{'old': 'return False',
                               'new': 'return ' + repaired.split('return ', 1)[1].strip()}]},
            {'action': 'run_check'},
            finish('synthetic repaired build'),
            {'action': 'run_check'}, finish('synthetic tester'),
            {'action': 'run_check'}, finish('synthetic reviewer'),
        ])
        sent = []

        class ScriptedConnection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, path, body, headers):
                sent.append(json.loads(body))

            def getresponse(self):
                action = next(actions)
                body = json.dumps({'choices': [{'message': {'content': json.dumps(action)}}]}).encode()
                return response(body)

            def close(self):
                pass

        directory = self.root / 'single-build'
        with patch.object(benchmark.http.client, 'HTTPConnection', ScriptedConnection), \
                patch.object(benchmark, 'runtime_snapshot', return_value={'offline': True}), \
                patch.object(benchmark.subprocess, 'check_output', return_value='offline-head\n'):
            report = benchmark.run_build(directory, self.freeze, self.pin)
        self.assertTrue(report['success'], report)
        self.assertFalse(report['first_attempt_success'])
        self.assertEqual(report['repair_loops'], 1)
        self.assertEqual(report['replace_actions'], 1)
        self.assertEqual(report['model_calls'], 12)
        self.assertEqual(report['transport_attempts'], 12)
        self.assertEqual((directory / 'builder-end-1.txt').read_text(), initial)
        self.assertEqual((directory / 'builder-end-2.txt').read_text(), repaired)
        self.assertEqual([item['host_check'][0] for item in report['builder_snapshots']], [False, True])
        self.assertEqual([item['attempt'] for item in report['builder_snapshots']], [1, 2])
        feedback = json.loads(sent[4]['messages'][2]['content'])
        self.assertEqual(feedback['kind'], 'host_check_feedback')
        self.assertIs(feedback['trusted'], True)
        self.assertIn('python_function_cases', feedback['output'])
        self.assertEqual(json.loads((directory / 'task.json').read_text()), self.task)
        self.assertNotIn('return ', report['task']['objective'])


if __name__ == '__main__':
    unittest.main()
