"""Fresh requirements builds with fixed conditions and preserved evidence."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_execution_contract import RecordedChat, runtime_snapshot
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.task_runner import parse_task_spec, run_forge_task, verify_forge_task, format_run_report


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    if json.loads(path.read_text(encoding='utf-8')) != value:
        raise ValueError('report readback mismatch')


def task_data():
    proposal = (ROOT / 'FORGE_REMINDER_BUILD_PROPOSAL.md').read_text(encoding='utf-8')
    objective = proposal.split('## Vorgeschlagener verbindlicher Bauinput\n\n')[1].split('\n## Feste')[0]
    rows = [line.split('|')[1:4] for line in proposal.splitlines()
            if line.startswith('| ') and not line.startswith(('| state ', '| ---'))]
    cases = [{'args': [json.loads(a.strip()), json.loads(b.strip())],
              'expected': json.loads(c.strip())} for a, b, c in rows]
    objective += '\nFeste Anforderungsfälle (args -> expected):\n' + json.dumps(cases, ensure_ascii=True)
    return {'task_id': 'forge-reminder-first-build', 'objective': objective,
            'allowed_files': ['agent/reminder.py'], 'checks': [
                {'kind': 'file_exists', 'path': 'agent/reminder.py'},
                {'kind': 'python_syntax', 'path': 'agent/reminder.py'},
                {'kind': 'python_function_cases', 'path': 'agent/reminder.py',
                 'function': 'should_remind', 'cases': cases}]}


def hashes():
    paths = list((ROOT / 'src').rglob('*.py')) + [Path(__file__),
             ROOT / 'scripts/benchmark_execution_contract.py',
             ROOT / 'scripts/verify_dialogue_host_gate.py',
             ROOT / 'FORGE_REMINDER_BUILD_PROPOSAL.md']
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted(paths)}


def validate_repeat_baseline():
    initial = ROOT / 'workspaces/forge-reminder-first-build-20261004/result.json'
    if digest(initial.read_bytes()) != '98c066aaee507dc0ae1593e626ecf062950edb3c3180ee4bfeadb71a04a59e93':
        raise ValueError('accepted initial result changed')
    baseline = json.loads(initial.read_text(encoding='utf-8'))
    current = hashes()
    prior = dict(baseline['hashes'])
    # Only this measurement script changes to select three fresh output paths.
    current.pop('scripts/benchmark_reminder_build.py')
    prior.pop('scripts/benchmark_reminder_build.py')
    if current != prior or task_data() != baseline['task']:
        raise ValueError('initial task or host/model source conditions changed')
    if not baseline['success'] or not baseline['first_attempt_success']:
        raise ValueError('initial build not mechanically accepted')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--repeat', type=int, choices=(1, 2, 3))
    args = parser.parse_args()
    if args.prepare == args.run:
        parser.error('select exactly one of --prepare / --run')
    task = task_data()
    if len(task['checks'][-1]['cases']) != 20:
        raise ValueError('expected twenty fixed cases')
    spec = parse_task_spec(json.dumps(task))
    if args.repeat is not None:
        validate_repeat_baseline()
    freeze_path = ROOT / ('workspaces/reminder-repeat-freeze-20261004.json'
                          if args.repeat is not None else 'workspaces/reminder-build-freeze-v2-20261004.json')
    if args.prepare:
        if freeze_path.exists():
            raise ValueError('never overwrite freeze')
        save(freeze_path, {'task': task, 'hashes': hashes()})
        print('PREPARED_ONLY: parsed fixed task, no model call')
        return 0
    frozen = json.loads(freeze_path.read_text(encoding='utf-8'))
    if frozen != {'task': task, 'hashes': hashes()}:
        raise ValueError('frozen conditions changed')
    if args.repeat is not None and args.repeat > 1:
        previous = ROOT / f'workspaces/forge-reminder-repeat-{args.repeat-1}-20261004/result.json'
        previous_result = json.loads(previous.read_text(encoding='utf-8'))
        if not previous_result['success'] or previous_result['hashes'] != frozen['hashes']:
            raise ValueError('previous repetition failed or conditions changed; stop series')
    directory = ROOT / (f'workspaces/forge-reminder-repeat-{args.repeat}-20261004'
                        if args.repeat is not None else 'workspaces/forge-reminder-first-build-20261004')
    directory.mkdir()
    save(directory / 'task.json', task)
    report = {'approved': False, 'success': False, 'task': task, 'hashes': frozen['hashes'],
              'independent_semantic_approval': 'pending', 'success_scope': 'mechanical_only',
              'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'manual_interventions': 0, 'transport_attempts': 0,
              'model': 'forge-30b', 'temperature': 0, 'role_events': []}
    save(directory / 'result.json', report)
    try:
        report['runtime_before'] = runtime_snapshot()
    except Exception as exc:
        report['preflight_error'] = f'{type(exc).__name__}: {exc}'
        save(directory / 'result.json', report)
        raise
    def transport(request, timeout):
        report['transport_attempts'] += 1
        number = report['transport_attempts']
        (directory / f'request-{number:03}.json').write_bytes(request.data)
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
        (directory / f'http-response-{number:03}.json').write_bytes(raw)
        return io.BytesIO(raw)
    chat = RecordedChat(LMStudioChatClient('forge-30b', timeout_seconds=300, opener=transport), directory)
    def progress(role, event):
        record = {'role': role, 'event': event, 'seconds': round(time.monotonic()-started, 3)}
        if role == 'builder' and event in ('passed', 'failed'):
            record['host_check'] = list(verify_forge_task(directory, spec))
            path = directory / 'agent/reminder.py'
            if path.is_file():
                raw = path.read_bytes()
                snapshot = directory / f'builder-end-{chat.builder_starts}.txt'
                snapshot.write_bytes(raw)
                record['artifact_sha256'] = digest(raw)
        report['role_events'].append(record)
        save(directory / 'result.json', report)
    started = time.monotonic()
    try:
        pipeline = run_forge_task(directory, spec, chat, max_steps=20, max_file_chars=20000,
                                  max_call_seconds=300, max_elapsed_seconds=900,
                                  max_repair_attempts=1, on_progress=progress)
        report['pipeline'] = json.loads(format_run_report(pipeline))
        report['approved'] = pipeline.approved
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        chat.close(report.get('error'))
        report.update(build_seconds=round(time.monotonic()-started, 3), model_calls=chat.model_calls,
                      repair_loops=max(0, chat.builder_starts-1), replace_actions=chat.repair_actions)
        report['independent_passed'], report['independent_detail'] = verify_forge_task(directory, spec)
        report['conditions_unchanged'] = hashes() == frozen['hashes'] and task_data() == task
        path = directory / 'agent/reminder.py'
        report['artifact_sha256'] = digest(path.read_bytes()) if path.is_file() else None
        try:
            report['runtime_after'] = runtime_snapshot()
        except Exception as exc:
            report['runtime_after_error'] = f'{type(exc).__name__}: {exc}'
        report['success'] = bool(report['approved'] and report['independent_passed'] and
                                 report['conditions_unchanged'] and 'runtime_after' in report)
        report['first_attempt_success'] = report['success'] and report['repair_loops'] == 0
        save(directory / 'result.json', report)
    print(json.dumps({k: report.get(k) for k in ('success', 'first_attempt_success', 'model_calls',
          'repair_loops', 'build_seconds', 'error', 'runtime_after_error')}))
    return 0 if report['success'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
