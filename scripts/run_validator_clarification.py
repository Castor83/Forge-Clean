"""One frozen local copy diagnosis. No generated action or code is executed."""
import argparse
from datetime import datetime, timezone
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import prepare_validator_clarification as prep
from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.tool_loop import ReplaceTextAction, parse_tool_action

PREPARATION = ROOT / 'workspaces/validator-clarification-preparation-v2-20261004'
PREPARATION_PIN = '2acba3b3fc7651f350c72321b95c2245da32896744e033a4f5f5cdfb4cd81d0a'
RUN = ROOT / 'workspaces/validator-clarification-live-20261005'
RUN_CONFIG = {'host_wait_seconds': 300, 'raw_response_limit_bytes': 1000000,
              'transport': 'direct_http_no_proxy_no_redirect_no_retry',
              'model_calls_max': 1, 'transport_attempts_max': 1}
SOURCES = ('scripts/run_validator_clarification.py',
           'tests/test_forge_validator_clarification_live.py')


def write_bytes(path, raw):
    path.write_bytes(raw)
    if path.read_bytes() != raw:
        raise ValueError('evidence readback mismatch: '+path.name)


def save_json(path, value):
    write_bytes(path, prep.encode(value))


def reserve_once(path, value):
    raw = prep.encode(value)
    with path.open('xb') as stream:
        stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError('reservation readback mismatch')


def verify_live(directory, pin, candidate_request=None):
    raw = (directory/'run-freeze.json').read_bytes()
    if prep.digest(raw) != pin:
        raise ValueError('live freeze pin changed')
    frozen = json.loads(raw)
    if (frozen['preparation_pin'] != PREPARATION_PIN
            or frozen['run_configuration'] != RUN_CONFIG
            or set(frozen['source_sha256']) != set(SOURCES)):
        raise ValueError('live contract changed')
    for name, expected in frozen['source_sha256'].items():
        if prep.digest((ROOT/name).read_bytes()) != expected:
            raise ValueError('live source changed: '+name)
    request = (PREPARATION/'request.json').read_bytes()
    prep.verify_preparation(PREPARATION, PREPARATION_PIN,
                            request if candidate_request is None else candidate_request,
                            prep.CONFIG)
    return frozen


def prepare_live(directory):
    prep.verify_preparation(PREPARATION, PREPARATION_PIN,
                            (PREPARATION/'request.json').read_bytes(), prep.CONFIG)
    directory.mkdir()  # Never reuse or overwrite a previous attempt.
    frozen = {'kind': 'single_clarification_live', 'preparation_pin': PREPARATION_PIN,
              'run_configuration': RUN_CONFIG,
              'source_sha256': {name: prep.digest((ROOT/name).read_bytes()) for name in SOURCES}}
    raw = prep.encode(frozen)
    write_bytes(directory/'run-freeze.json', raw)
    pin = prep.digest(raw)
    verify_live(directory, pin)
    print('LIVE_PREPARED_ONLY MODEL_CALLS=0 RUN_FREEZE_SHA256='+pin)


class GuardedTransport:
    def __init__(self, directory, pin):
        self.directory, self.pin = directory, pin

    def __call__(self, request, timeout):
        if (request.full_url != prep.CONFIG['endpoint'] or request.get_method() != 'POST'
                or timeout != prep.CONFIG['timeout_seconds']
                or request.get_header('Content-type') != 'application/json'):
            raise ValueError('actual transport configuration changed')
        # Validate actual outgoing bytes immediately before the single transport.
        request_raw = bytes(request.data)
        verify_live(self.directory, self.pin, request_raw)
        reserve_once(self.directory/'transport-reserved.json', {'attempt': 1})
        write_bytes(self.directory/'request.json', request_raw)
        endpoint = urlparse(request.full_url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=timeout)
        try:
            connection.request('POST', endpoint.path, body=request_raw,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = bytearray()
            read_error = None
            try:
                while not response.isclosed() and len(raw) <= RUN_CONFIG['raw_response_limit_bytes']:
                    chunk = response.read1(min(65536, RUN_CONFIG['raw_response_limit_bytes']+1-len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    write_bytes(self.directory/'response-http-body.json', bytes(raw))
            except http.client.IncompleteRead as exc:
                raw.extend(exc.partial)
                read_error = 'IncompleteRead: '+str(exc)
            except (OSError, TimeoutError) as exc:
                read_error = type(exc).__name__+': '+str(exc)
            raw = bytes(raw)
            write_bytes(self.directory/'response-http-body.json', raw)
            declared = response.getheader('Content-Length')
            length_valid = (declared is None or (declared.strip().isdigit()
                            and len(raw) == int(declared.strip())))
            complete = (read_error is None and response.isclosed() and response.length in (None, 0)
                        and length_valid and len(raw) <= RUN_CONFIG['raw_response_limit_bytes'])
            save_json(self.directory/'http-metadata.json',
                      {'status': response.status, 'body_complete': complete,
                       'body_sha256': prep.digest(raw),
                       'content_length': declared, 'read_error': read_error,
                       'content_type': response.getheader('Content-Type')})
            if not complete:
                raise ValueError('raw response incomplete or byte limit exceeded')
            if response.status != 200:
                raise ValueError('HTTP status '+str(response.status)+'; no redirect or retry')
            return io.BytesIO(raw)
        finally:
            connection.close()


def worker(directory, pin):
    verify_live(directory, pin)
    admission = json.loads((directory/'execution-started.json').read_text(encoding='utf-8'))
    if admission.get('parent_pid') != os.getppid() or admission.get('run_freeze_pin') != pin:
        raise ValueError('worker requires the owning bounded parent')
    # This validated snapshot is within the parent's hard wait window.
    save_json(directory/'runtime-before.json', benchmark.runtime_snapshot())
    messages = tuple(tuple(item) for item in json.loads(
        (PREPARATION/'prepared-messages.json').read_text(encoding='utf-8')))
    client = LMStudioChatClient(prep.CONFIG['model'], endpoint=prep.CONFIG['endpoint'],
                               timeout_seconds=prep.CONFIG['timeout_seconds'],
                               opener=GuardedTransport(directory, pin),
                               execution_copy_field_order=True)
    client.set_tool_actions(tuple(prep.CONFIG['actions']))
    reserve_once(directory/'model-call-reserved.json', {'model_call': 1})
    response = client(messages)  # Exactly one call. No action application or retry.
    write_bytes(directory/'response.txt', response.encode('utf-8'))


def bounded_process(command, directory, limit_seconds):
    started = time.monotonic()
    with (directory/'worker-stdout.txt').open('wb') as stdout, \
            (directory/'worker-stderr.txt').open('wb') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
        timed_out = False
        try:
            process.wait(timeout=max(0, limit_seconds-(time.monotonic()-started)))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()  # Only this host-owned child; never the model/server.
            process.wait(timeout=10)
    elapsed = time.monotonic()-started
    return {'worker_exit': process.returncode, 'worker_seconds': round(elapsed, 3),
            'hard_timeout': timed_out, 'within_deadline': not timed_out and elapsed < limit_seconds}


def assess_response(raw, payload):
    action = parse_tool_action(raw.decode('utf-8'))
    marker = benchmark.REFERENCE_APPEND_MARKER
    facts = {'parsed_action_type': type(action).__name__, 'path_matches': False,
             'old_matches': False, 'complete_payload_matches': False}
    if type(action) is ReplaceTextAction:
        facts.update(path_matches=action.path == 'agent/execution_contract.py',
                     old_matches=action.old.encode('utf-8') == marker,
                     complete_payload_matches=action.new.encode('utf-8') == payload,
                     old_chars=len(action.old), new_chars=len(action.new),
                     new_sha256=prep.digest(action.new.encode('utf-8')))
    facts['copy_passed'] = all(facts[key] for key in
                             ('path_matches', 'old_matches', 'complete_payload_matches'))
    return facts


def execute(directory, pin):
    verify_live(directory, pin)
    reserve_once(directory/'execution-started.json', {'utc': datetime.now(timezone.utc).isoformat(),
                                                     'parent_pid': os.getpid(), 'run_freeze_pin': pin})
    report = {'kind': 'diagnostic_not_build', 'preparation_pin': PREPARATION_PIN,
              'run_freeze_pin': pin, 'run_configuration': RUN_CONFIG,
              'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                                   text=True).strip(),
              'diagnostic_passed': False, 'generated_file_writes': 0,
              'generated_code_executions': 0, 'independent_review_required': True}
    try:
        verify_live(directory, pin)
        report.update(bounded_process([sys.executable, str(Path(__file__).resolve()),
                                       '--worker', '--run-freeze-pin', pin], directory,
                                      RUN_CONFIG['host_wait_seconds']))
        if report['worker_exit'] != 0 or not report['within_deadline']:
            raise ValueError('worker failed or hard deadline exceeded; response not accepted')
        runtime_before = json.loads((directory/'runtime-before.json').read_text(encoding='utf-8'))
        benchmark.validate_runtime(runtime_before)
        report['runtime_before'] = runtime_before
        metadata = json.loads((directory/'http-metadata.json').read_text(encoding='utf-8'))
        http_raw = (directory/'response-http-body.json').read_bytes()
        response = (directory/'response.txt').read_bytes()
        content = json.loads(http_raw)['choices'][0]['message']['content']
        if (metadata['status'] != 200 or metadata['body_complete'] is not True
                or metadata['body_sha256'] != prep.digest(http_raw)
                or content.encode('utf-8') != response
                or (directory/'request.json').read_bytes() != (PREPARATION/'request.json').read_bytes()):
            raise ValueError('raw request/response evidence mismatch')
        report.update(assess_response(response, (PREPARATION/'payload.txt').read_bytes()))
        report['raw_evidence_complete'] = True
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
    finally:
        report['model_call_reservations'] = int((directory/'model-call-reserved.json').exists())
        report['transport_attempts'] = int((directory/'transport-reserved.json').exists())
        try:
            report['runtime_after'] = benchmark.runtime_snapshot()
            report['runtime_after_valid'] = True
        except Exception as exc:
            report['runtime_after_error'] = type(exc).__name__+': '+str(exc)
        try:
            verify_live(directory, pin)
            report['conditions_unchanged'] = True
        except Exception as exc:
            report['conditions_error'] = type(exc).__name__+': '+str(exc)
        report['diagnostic_passed'] = (report.get('copy_passed') is True
            and report.get('within_deadline') is True and report.get('worker_exit') == 0
            and report.get('raw_evidence_complete') is True
            and report.get('runtime_after_valid') is True
            and report.get('conditions_unchanged') is True
            and report['model_call_reservations'] == report['transport_attempts'] == 1
            and 'error' not in report)
        report['evidence_sha256'] = {path.name: prep.digest(path.read_bytes())
                                    for path in directory.iterdir() if path.is_file()
                                    and path.name != 'result.json'}
        save_json(directory/'result.json', report)
    print(json.dumps({key: report.get(key) for key in ('diagnostic_passed', 'worker_seconds',
          'transport_attempts', 'old_chars', 'new_chars', 'complete_payload_matches', 'error')}))
    return 0 if report['diagnostic_passed'] else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare-live', action='store_true')
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--worker', action='store_true')
    parser.add_argument('--run-freeze-pin')
    args = parser.parse_args()
    if args.prepare_live:
        prepare_live(RUN)
    elif not args.run_freeze_pin:
        parser.error('an independently checked run-freeze pin is required')
    elif args.worker:
        worker(RUN, args.run_freeze_pin)
    else:
        raise SystemExit(execute(RUN, args.run_freeze_pin))
