"""Offline behavioral evidence for single-attempt transport and host deadline."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.request import Request

from scripts import run_validator_clarification as live


class ClarificationLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def http_response(self, body, status=200, declared_length=None):
        declared = len(body) if declared_length is None else declared_length
        raw = ('HTTP/1.1 '+str(status)+' Test\r\nContent-Type: application/json\r\nContent-Length: '
               +str(declared)+'\r\n\r\n').encode()+body
        socket_stub = Mock()
        socket_stub.makefile.return_value = io.BytesIO(raw)
        response = live.http.client.HTTPResponse(socket_stub)
        response.begin()
        return response

    def test_attempt_reservation_prevents_second_attempt_and_keeps_first(self):
        path = self.directory/'attempt.json'
        live.reserve_once(path, {'attempt': 1})
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            live.reserve_once(path, {'attempt': 2})
        self.assertEqual(path.read_bytes(), original)

    def test_real_host_timeout_kills_only_owned_stub_before_late_output(self):
        late = self.directory/'late.txt'
        command = [sys.executable, '-c',
                   'import time,pathlib; time.sleep(5); pathlib.Path('+repr(str(late))+').write_text("late")']
        result = live.bounded_process(command, self.directory, 0.3)
        self.assertTrue(result['hard_timeout'])
        self.assertFalse(result['within_deadline'])
        self.assertNotEqual(result['worker_exit'], 0)
        self.assertFalse(late.exists())
        self.assertLess(result['worker_seconds'], 4)

    def test_real_completed_stub_and_nonzero_exit_are_distinguished(self):
        ok = live.bounded_process([sys.executable, '-c', 'print("saved")'], self.directory, 10)
        self.assertEqual(ok['worker_exit'], 0)
        self.assertTrue(ok['within_deadline'])
        self.assertIn(b'saved', (self.directory/'worker-stdout.txt').read_bytes())
        failed = live.bounded_process([sys.executable, '-c', 'raise SystemExit(7)'], self.directory, 10)
        self.assertEqual(failed['worker_exit'], 7)

    def test_transport_guards_actual_bytes_before_http_and_never_retries_redirect(self):
        request = Request(live.prep.CONFIG['endpoint'], data=b'actual-bytes',
                          headers={'Content-Type': 'application/json'}, method='POST')
        body = b'complete redirect response'
        response = self.http_response(body, status=302)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(live, 'verify_live') as guard, \
                patch.object(live.http.client, 'HTTPConnection', return_value=connection):
            transport = live.GuardedTransport(self.directory, 'fixed-pin')
            with self.assertRaisesRegex(ValueError, 'no redirect or retry'):
                transport(request, 300)
            guard.assert_called_once_with(self.directory, 'fixed-pin', b'actual-bytes')
            self.assertEqual(connection.request.call_count, 1)
            self.assertEqual((self.directory/'response-http-body.json').read_bytes(), body)
            with self.assertRaises(FileExistsError):
                transport(request, 300)
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()

    def test_short_http_body_is_rejected_even_when_its_json_is_valid(self):
        body = b'{"choices":[{"message":{"content":"valid"}}]}'
        connection = Mock()
        connection.getresponse.return_value = self.http_response(body, declared_length=len(body)+5)
        request = Request(live.prep.CONFIG['endpoint'], data=b'fixed',
                          headers={'Content-Type': 'application/json'}, method='POST')
        with patch.object(live, 'verify_live'), \
                patch.object(live.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'raw response incomplete'):
                live.GuardedTransport(self.directory, 'pin')(request, 300)
        self.assertEqual((self.directory/'response-http-body.json').read_bytes(), body)
        metadata = json.loads((self.directory/'http-metadata.json').read_text())
        self.assertFalse(metadata['body_complete'])
        self.assertEqual(connection.request.call_count, 1)

    def test_complete_http_body_and_frozen_request_bytes_are_preserved(self):
        body = b'{"choices":[{"message":{"content":"valid"}}]}'
        connection = Mock()
        connection.getresponse.return_value = self.http_response(body)
        request = Request(live.prep.CONFIG['endpoint'], data=b'fixed',
                          headers={'Content-Type': 'application/json'}, method='POST')
        def guarded_then_mutated(directory, pin, actual):
            self.assertEqual(actual, b'fixed')
            request.data = b'mutated after guard'
        with patch.object(live, 'verify_live', side_effect=guarded_then_mutated), \
                patch.object(live.http.client, 'HTTPConnection', return_value=connection):
            result = live.GuardedTransport(self.directory, 'pin')(request, 300)
            self.assertEqual(result.read(), body)
        self.assertEqual(connection.request.call_args.kwargs['body'], b'fixed')
        self.assertEqual((self.directory/'request.json').read_bytes(), b'fixed')
        self.assertTrue(json.loads((self.directory/'http-metadata.json').read_text())['body_complete'])

    def test_guard_failure_prevents_connection_and_transport_reservation(self):
        request = Request(live.prep.CONFIG['endpoint'], data=b'changed',
                          headers={'Content-Type': 'application/json'}, method='POST')
        with patch.object(live, 'verify_live', side_effect=ValueError('candidate differs')), \
                patch.object(live.http.client, 'HTTPConnection') as connection:
            with self.assertRaisesRegex(ValueError, 'candidate differs'):
                live.GuardedTransport(self.directory, 'pin')(request, 300)
            connection.assert_not_called()
            self.assertFalse((self.directory/'transport-reserved.json').exists())

    def test_body_timeout_preserves_partial_bytes_and_stays_negative(self):
        response = Mock(status=200, length=99)
        response.isclosed.return_value = False
        response.read1.side_effect = [b'partial body', TimeoutError('stalled')]
        response.getheader.side_effect = lambda name: '111' if name == 'Content-Length' else 'application/json'
        connection = Mock()
        connection.getresponse.return_value = response
        request = Request(live.prep.CONFIG['endpoint'], data=b'fixed',
                          headers={'Content-Type': 'application/json'}, method='POST')
        with patch.object(live, 'verify_live'), \
                patch.object(live.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(ValueError, 'raw response incomplete'):
                live.GuardedTransport(self.directory, 'pin')(request, 300)
        self.assertEqual((self.directory/'response-http-body.json').read_bytes(), b'partial body')
        metadata = json.loads((self.directory/'http-metadata.json').read_text())
        self.assertFalse(metadata['body_complete'])
        self.assertIn('TimeoutError', metadata['read_error'])

    def test_late_worker_output_can_never_be_accepted_as_success(self):
        deadline = {'worker_exit': 0, 'worker_seconds': 301, 'hard_timeout': True, 'within_deadline': False}
        with patch.object(live, 'verify_live'), patch.object(live, 'bounded_process', return_value=deadline), \
                patch.object(live.benchmark, 'runtime_snapshot', return_value=[]), \
                patch.object(live.subprocess, 'check_output', return_value='fixture-head'), \
                patch.object(live, 'assess_response') as assess, patch('builtins.print'):
            self.assertEqual(live.execute(self.directory, 'pin'), 2)
            assess.assert_not_called()
        report = json.loads((self.directory/'result.json').read_text())
        self.assertFalse(report['diagnostic_passed'])
        self.assertIn('hard deadline', report['error'])

    def test_direct_worker_without_bounded_parent_is_rejected_before_runtime(self):
        live.save_json(self.directory/'execution-started.json', {'parent_pid': -1, 'run_freeze_pin': 'pin'})
        with patch.object(live, 'verify_live'), patch.object(live.benchmark, 'runtime_snapshot') as runtime:
            with self.assertRaisesRegex(ValueError, 'owning bounded parent'):
                live.worker(self.directory, 'pin')
            runtime.assert_not_called()
            self.assertFalse((self.directory/'model-call-reserved.json').exists())

    def test_exact_payload_is_required_and_marker_omission_remains_negative(self):
        payload = b'original\n'+live.benchmark.REFERENCE_APPEND_MARKER
        data = {'action': 'replace_text', 'path': 'agent/execution_contract.py',
                'old': live.benchmark.REFERENCE_APPEND_MARKER.decode(), 'new': payload.decode()}
        self.assertTrue(live.assess_response(json.dumps(data).encode(), payload)['copy_passed'])
        data['new'] = 'original\n'
        self.assertFalse(live.assess_response(json.dumps(data).encode(), payload)['copy_passed'])

    def test_live_source_config_and_external_freeze_pin_cannot_drift(self):
        source = self.directory/'runner.py'
        source.write_bytes(b'original')
        frozen = {'preparation_pin': live.PREPARATION_PIN, 'run_configuration': live.RUN_CONFIG,
                  'source_sha256': {'runner.py': live.prep.digest(source.read_bytes())}}
        raw = live.prep.encode(frozen)
        (self.directory/'run-freeze.json').write_bytes(raw)
        (self.directory/'request.json').write_bytes(b'fixed')
        with patch.object(live, 'ROOT', self.directory), patch.object(live, 'SOURCES', ('runner.py',)), \
                patch.object(live, 'PREPARATION', self.directory), \
                patch.object(live.prep, 'verify_preparation') as guard:
            pin = live.prep.digest(raw)
            live.verify_live(self.directory, pin, b'actual')
            guard.assert_called_once_with(self.directory, live.PREPARATION_PIN, b'actual', live.prep.CONFIG)
            source.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'live source changed'):
                live.verify_live(self.directory, pin)
            with self.assertRaisesRegex(ValueError, 'live freeze pin changed'):
                live.verify_live(self.directory, 'wrong-pin')


if __name__ == '__main__':
    unittest.main()
