"""One requirements build with fixed inputs, bounded roles and raw evidence."""
import argparse
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_execution_contract import RecordedChat, runtime_snapshot
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.task_runner import parse_task_spec, run_forge_task, verify_forge_task, format_run_report

TASK = 'examples/forge_retry_decision.json'
OUTPUT = 'agent/retry_decision.py'
FREEZE = ROOT / 'workspaces/retry-build-freeze-20261005.json'
RUN = ROOT / 'workspaces/forge-retry-first-build-20261005'
CONFIG = {'model': 'forge-30b', 'temperature': 0, 'context_length': 32768, 'parallel': 1,
          'endpoint': 'http://127.0.0.1:1234/v1/chat/completions',
          'max_steps': 20, 'max_file_chars': 20000, 'max_call_seconds': 300,
          'max_elapsed_seconds': 900, 'max_repair_attempts': 1,
          'raw_response_limit_bytes': 1000000,
          'transport': 'direct_http_no_proxy_no_redirect_no_retry'}
REPAIR_OBJECTIVE = ('Apply only the host_check_feedback corrections to the '
                    'existing allowed files with one localized '
                    'replace_text_batch action per affected file, then run the '
                    'host check.')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_bytes(path, raw, exclusive=False):
    with path.open('xb' if exclusive else 'wb') as stream:
        stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError('evidence readback mismatch: ' + path.name)


def save(path, value, exclusive=False):
    write_bytes(path, (json.dumps(value, ensure_ascii=True, indent=2) + '\n').encode(), exclusive)


def task_data():
    task = json.loads((ROOT / TASK).read_text(encoding='utf-8'))
    parse_task_spec(json.dumps(task))
    checks = task['checks']
    if (task['task_id'] != 'forge-retry-first-build' or task['allowed_files'] != [OUTPUT]
            or [c['kind'] for c in checks] != ['file_exists', 'python_syntax', 'python_function_cases']
            or any(c['path'] != OUTPUT for c in checks)
            or checks[-1]['function'] != 'should_retry'):
        raise ValueError('retry task boundary changed')
    cases = checks[-1]['cases']
    if (len(cases) != 27 or any(type(c['expected']) is not bool for c in cases)
            or sum(c['expected'] for c in cases) != 3):
        raise ValueError('expected 27 fixed cases with three true bool results')
    return task


def source_hashes():
    paths = list((ROOT / 'src').rglob('*.py'))
    paths += [ROOT / name for name in (
        TASK, 'AGENTS.md', 'FORGE_NEXT_BUILD_PROPOSAL.md',
        'scripts/benchmark_retry_build.py', 'scripts/benchmark_execution_contract.py',
        'scripts/verify_dialogue_host_gate.py', 'tests/test_forge_retry_build_guards.py')]
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted(paths)}


def current_state():
    return {'task': task_data(), 'configuration': dict(CONFIG), 'sources': source_hashes()}


def prepare(freeze=FREEZE):
    save(freeze, current_state(), exclusive=True)
    return digest(freeze.read_bytes())


def guard(freeze, pin):
    raw = freeze.read_bytes()
    if digest(raw) != pin:
        raise ValueError('externally pinned freeze changed')
    frozen = json.loads(raw)
    if frozen != current_state():
        raise ValueError('frozen task, source inventory or configuration changed')
    return frozen


def validate_request(request, timeout, task, repair_feedback=None):
    raw = bytes(request.data)
    if (request.full_url != CONFIG['endpoint'] or request.get_method() != 'POST'
            or request.get_header('Content-type') != 'application/json'
            or timeout != CONFIG['max_call_seconds']):
        raise ValueError('actual transport configuration changed')
    payload = json.loads(raw)
    handoff = json.loads(payload['messages'][1]['content'])
    role = handoff['role']
    expected = {'task_id': task['task_id'], 'objective': task['objective'],
                'allowed_files': task['allowed_files'], 'role': role,
                'completed_roles': {'builder': [], 'tester': ['builder'],
                                    'reviewer': ['builder', 'tester']}.get(role)}
    if handoff.get('objective') == REPAIR_OBJECTIVE:
        feedback = json.loads(payload['messages'][2]['content'])
        if (role != 'builder' or repair_feedback is None or feedback != {
                'kind': 'host_check_feedback', 'trusted': True, 'output': repair_feedback}):
            raise ValueError('repair request lacks bound negative host feedback')
        expected.pop('completed_roles')
        expected['objective'] = REPAIR_OBJECTIVE
    if (payload['model'] != CONFIG['model'] or payload['temperature'] != 0
            or payload['stream'] is not False
            or role not in ('builder', 'tester', 'reviewer') or handoff != expected):
        raise ValueError('actual outgoing model or task changed')
    return raw


def receive_response(response, path):
    raw = bytearray()
    error = None
    try:
        while not response.isclosed() and len(raw) <= CONFIG['raw_response_limit_bytes']:
            chunk = response.read1(min(65536, CONFIG['raw_response_limit_bytes'] + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            write_bytes(path, bytes(raw))
    except http.client.IncompleteRead as exc:
        raw.extend(exc.partial)
        error = 'IncompleteRead: ' + str(exc)
    except (OSError, TimeoutError) as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    raw = bytes(raw)
    write_bytes(path, raw)
    declared = response.getheader('Content-Length')
    complete = (error is None and response.isclosed() and response.length in (None, 0)
                and len(raw) <= CONFIG['raw_response_limit_bytes']
                and (declared is None or (declared.strip().isdigit()
                     and len(raw) == int(declared.strip()))))
    return raw, {'status': response.status, 'body_complete': complete, 'read_error': error,
                 'content_length': declared, 'body_sha256': digest(raw)}


class GuardedTransport:
    def __init__(self, directory, freeze, pin):
        self.directory, self.freeze, self.pin = directory, freeze, pin
        self.attempts, self.failed = 0, False
        self.repair_feedback = None
        self.lock = threading.Lock()

    def __call__(self, request, timeout):
        if not self.lock.acquire(blocking=False):
            self.failed = True
            raise ValueError('parallel transport refused')
        connection = None
        try:
            if self.failed:
                raise ValueError('transport stopped after earlier failure')
            frozen = guard(self.freeze, self.pin)
            raw = validate_request(request, timeout, frozen['task'], self.repair_feedback)
            self.attempts += 1
            number = self.attempts
            write_bytes(self.directory / f'request-{number:03}.json', raw, exclusive=True)
            connection = http.client.HTTPConnection('127.0.0.1', 1234, timeout=timeout)
            connection.request('POST', '/v1/chat/completions', body=raw,
                               headers={'Content-Type': 'application/json'})
            body, metadata = receive_response(connection.getresponse(),
                                              self.directory / f'http-response-{number:03}.json')
            save(self.directory / f'http-metadata-{number:03}.json', metadata)
            if metadata['status'] != 200 or not metadata['body_complete']:
                raise ValueError('HTTP failure or incomplete response; no retry')
            return io.BytesIO(body)
        except BaseException:
            self.failed = True
            raise
        finally:
            if connection is not None:
                connection.close()
            self.lock.release()


def run_build(directory, freeze, pin):
    frozen = guard(freeze, pin)
    spec = parse_task_spec(json.dumps(frozen['task']))
    directory.mkdir()  # No overwrite, resume or second fresh build in this run.
    save(directory / 'task.json', frozen['task'], exclusive=True)
    report = {'success': False, 'approved': False, 'first_attempt_success': False,
              'independent_semantic_approval': 'pending', 'success_scope': 'mechanical_only',
              'freeze_pin': pin, 'task': frozen['task'], 'configuration': frozen['configuration'],
              'sources': frozen['sources'], 'manual_interventions': 0, 'role_events': [],
              'builder_snapshots': [],
              'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}
    save(directory / 'result.json', report)
    transport = GuardedTransport(directory, freeze, pin)
    chat = RecordedChat(LMStudioChatClient(CONFIG['model'], timeout_seconds=300, opener=transport), directory)
    started = time.monotonic()

    def snapshot_builder():
        attempt = chat.builder_starts
        if attempt < 1 or any(s['attempt'] == attempt for s in report['builder_snapshots']):
            return
        try:
            host_check = list(verify_forge_task(directory, spec))
        except Exception as exc:
            host_check = [False, type(exc).__name__ + ': ' + str(exc)]
        snapshot = {'attempt': attempt, 'host_check': host_check}
        path = directory / OUTPUT
        if path.is_file():
            raw = path.read_bytes()
            write_bytes(directory / f'builder-end-{attempt}.txt', raw, exclusive=True)
            snapshot['artifact_sha256'] = digest(raw)
        report['builder_snapshots'].append(snapshot)

    def progress(role, event):
        # Also preserve a rejected first builder before a HostCheckFailure repair.
        if role == 'builder' and event in ('started', 'passed', 'failed'):
            snapshot_builder()
            if event == 'started' and chat.builder_starts == 1:
                first = report['builder_snapshots'][0]
                if first['host_check'][0] is False:
                    transport.repair_feedback = first['host_check'][1][:CONFIG['max_file_chars']]
        report['role_events'].append({'role': role, 'event': event,
                                     'seconds': round(time.monotonic() - started, 3)})
        save(directory / 'result.json', report)

    try:
        report['runtime_before'] = runtime_snapshot()
        pipeline = run_forge_task(directory, spec, chat, max_steps=CONFIG['max_steps'],
                                  max_file_chars=CONFIG['max_file_chars'],
                                  max_call_seconds=CONFIG['max_call_seconds'],
                                  max_elapsed_seconds=CONFIG['max_elapsed_seconds'],
                                  max_repair_attempts=CONFIG['max_repair_attempts'], on_progress=progress)
        report['pipeline'] = json.loads(format_run_report(pipeline))
        report['approved'] = pipeline.approved
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        chat.close(report.get('error'))
        snapshot_builder()
        report.update(build_seconds=round(time.monotonic() - started, 3),
                      model_calls=chat.model_calls, transport_attempts=transport.attempts,
                      transport_failed=transport.failed,
                      repair_loops=max(0, chat.builder_starts - 1), replace_actions=chat.repair_actions)
        try:
            report['independent_passed'], report['independent_detail'] = verify_forge_task(directory, spec)
        except Exception as exc:
            report['independent_passed'] = False
            report['independent_detail'] = type(exc).__name__ + ': ' + str(exc)
        try:
            report['conditions_unchanged'] = guard(freeze, pin) == frozen
        except Exception as exc:
            report['conditions_unchanged'] = False
            report['conditions_error'] = type(exc).__name__ + ': ' + str(exc)
        try:
            report['runtime_after'] = runtime_snapshot()
        except Exception as exc:
            report['runtime_after_error'] = type(exc).__name__ + ': ' + str(exc)
        path = directory / OUTPUT
        report['artifact_sha256'] = digest(path.read_bytes()) if path.is_file() else None
        report['success'] = bool(report['approved'] and report['independent_passed']
                                 and report['conditions_unchanged'] and 'runtime_after' in report
                                 and not transport.failed and chat.builder_starts in (1, 2)
                                 and chat.model_calls == transport.attempts and not chat.pending)
        report['first_attempt_success'] = report['success'] and report['repair_loops'] == 0
        report['evidence_sha256'] = {p.relative_to(directory).as_posix(): digest(p.read_bytes())
                                     for p in sorted(directory.rglob('*'))
                                     if p.is_file() and p.name != 'result.json'}
        save(directory / 'result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--run', action='store_true')
    parser.add_argument('--freeze-pin')
    args = parser.parse_args()
    if args.prepare:
        print('PREPARED_ONLY MODEL_CALLS=0 FREEZE_SHA256=' + prepare())
        return 0
    if not args.freeze_pin:
        parser.error('--run requires an externally checked --freeze-pin')
    report = run_build(RUN, FREEZE, args.freeze_pin)
    print(json.dumps({key: report.get(key) for key in (
        'success', 'first_attempt_success', 'model_calls', 'transport_attempts',
        'repair_loops', 'build_seconds', 'error', 'runtime_after_error')}))
    return 0 if report['success'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
