"""One initial build, then explicitly gated three-run frozen reproduction."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.benchmark_policy_service import MeasuredChat
from src.forge_runtime.capability_registry_contract import REFERENCE_SHA256
from src.forge_runtime.capability_registry_suite import DEPENDENCIES, REQUIRED_FILES, run_capability_registry
from src.forge_runtime.host_suites import run_host_suite
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.task_runner import parse_task_spec, run_forge_task, format_run_report
from src.forge_runtime.workspace_tools import WorkspaceFileTools

INPUT_SHA256 = dict(zip(DEPENDENCIES, (
    '9adf70f80f4d402070b69e770e1232938f2fa1a0e7bff8d77c76b7de621e8d4a',
    'dd99b23057367996c4777eba1ab959b0b88f6c5eae2ffb0886efc0afba03f36f',
    '73bd891d76bd8e6cd0b546f421e9a8748286d49677c513f1fc83bc7a47c8a7c2',
    'd85173bc5bca2d96ead47f0f375144428b7c77eadba56df415e370ff6003fdb3',
    '8dd59d1577d6364b2bd0c79e31862d29bf98c4009eb396367e40a9fd1b6cf6b2',
    '2e6da677a5b43e4f6214d0acee9bb8bec4d242440d2ffc7e1281b34736f7a352',
)))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def frozen_inputs():
    reference = ROOT / 'src/obsidian_policy/capability_registry.py'
    if digest(reference.read_bytes()) != REFERENCE_SHA256:
        raise ValueError('reference registry changed; new explicit contract required')
    inputs = {'SPEC.md': (ROOT / 'examples/forge_capability_registry_SPEC.md').read_bytes()}
    for name, expected in INPUT_SHA256.items():
        raw = (ROOT / 'workspaces/forge-policy-benchmark-20261001/run-1' / name).read_bytes()
        if digest(raw) != expected:
            raise ValueError(f'accepted Forge input changed: {name}')
        inputs[name] = raw
    return inputs


def verifier_hashes():
    paths = sorted((ROOT / 'src/forge_runtime').glob('*.py'))
    paths += sorted((ROOT / 'src/obsidian_policy').glob('*.py'))
    paths += [Path(__file__).resolve(), ROOT / 'scripts/benchmark_policy_service.py',
              ROOT / 'examples/forge_capability_registry.json',
              ROOT / 'FORGE_CAPABILITY_REGISTRY_REPRODUCTION_CONTRACT.md',
              ROOT / 'tests/test_forge_capability_registry_suite.py',
              ROOT / 'tests/test_forge_registry_active_mutation.py',
              ROOT / 'tests/test_forge_host_suites.py',
              ROOT / 'tests/test_forge_capability_registry_measurement.py']
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in paths}


def accepted_result(result):
    """A later single hostcheck cannot erase a failed pipeline or lost evidence."""
    return (all(result.get(key) is True for key in (
        'approved', 'independent_passed', 'inputs_unchanged', 'readback_passed', 'conditions_unchanged'))
        and type(result.get('artifact_sha256')) is dict
        and set(result['artifact_sha256']) == set(REQUIRED_FILES)
        and all(type(v) is str and re.fullmatch(r'[0-9a-f]{64}', v) for v in result['artifact_sha256'].values()))


class RawChat:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def set_tool_actions(self, actions):
        self.client.set_tool_actions(actions)

    def __call__(self, messages):
        record = {'messages': messages, 'started_utc': datetime.now(timezone.utc).isoformat()}
        try:
            response = self.client(messages)
            record['response'] = response
            return response
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=True) + '\n')


def conditions_match(inputs, hashes):
    try:
        return verifier_hashes() == hashes and frozen_inputs() == inputs
    except Exception:
        return False


def validate_initial_report(report_path, review_path, inputs, hashes):
    report_path = report_path.resolve()
    report_path.relative_to((ROOT / 'workspaces').resolve())
    review_path = review_path.resolve()
    review_path.relative_to((ROOT / 'workspaces').resolve())
    raw = report_path.read_bytes()
    first = json.loads(raw)
    review = json.loads(review_path.read_text(encoding='utf-8'))
    input_hashes = {name: digest(value) for name, value in inputs.items()}
    if (first.get('verifier_sha256') != hashes or first.get('input_sha256') != input_hashes
            or first.get('model') != 'forge-30b' or first.get('temperature') != 0
            or len(first.get('runs', [])) != 1 or not accepted_result(first['runs'][0])):
        raise ValueError('initial report lacks complete matching evidence')
    result = first['runs'][0]
    run = (ROOT / result['workspace']).resolve()
    run.relative_to(report_path.parent.resolve())
    result_raw = (run / 'result.json').read_bytes()
    if json.loads(result_raw) != result:
        raise ValueError('actual initial result differs from report')
    if (set(review) != {'kind', 'approved', 'reviewer', 'report_sha256', 'result_sha256', 'model_calls_sha256'}
            or review['kind'] != 'forge_registry_initial_review' or review['approved'] is not True
            or type(review['reviewer']) is not str or not review['reviewer'].strip()
            or review['report_sha256'] != digest(raw) or review['result_sha256'] != digest(result_raw)
            or review['model_calls_sha256'] != digest((run / 'model_calls.jsonl').read_bytes())):
        raise ValueError('initial independent review is missing or not bound to actual evidence')
    for name, original in inputs.items():
        if (run / name).read_bytes() != original:
            raise ValueError('actual initial read-only input changed')
    for name, expected in result['artifact_sha256'].items():
        if digest((run / name).read_bytes()) != expected:
            raise ValueError('actual initial output changed')
    if run_capability_registry({name: (run / name).read_text(encoding='utf-8') for name in REQUIRED_FILES}) is not None:
        raise ValueError('actual initial readback rejected')
    return True


def execute_run(root, spec, inputs, hashes, client):
    root.mkdir()
    for name, raw in inputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if path.read_bytes() != raw:
            raise ValueError('input copy readback mismatch')
    measured = MeasuredChat(RawChat(client, root / 'model_calls.jsonl'), root / 'trace.jsonl')
    started = time.monotonic()
    result = {'workspace': root.relative_to(ROOT).as_posix(), 'approved': False,
              'independent_passed': False, 'readback_passed': False, 'manual_interventions': 0}
    try:
        pipeline = run_forge_task(root, spec, measured, max_steps=20,
                                  max_call_seconds=300, max_elapsed_seconds=900, max_repair_attempts=1)
        result['pipeline'] = json.loads(format_run_report(pipeline))
        result['approved'] = pipeline.approved
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    result['build_seconds'] = round(time.monotonic() - started, 3)
    result['model_calls'] = measured.model_calls
    result['repair_loops'] = max(0, measured.builder_starts - 1)
    result['repair_actions'] = measured.repair_actions
    result['inputs_unchanged'] = all((root / p).is_file() and (root / p).read_bytes() == raw for p, raw in inputs.items())
    try:
        workspace = WorkspaceFileTools(root, spec.task)
        passed, detail = run_host_suite('capability_registry_v1', workspace)
        result['independent_passed'], result['independent_detail'] = passed, detail
        contents = {name: workspace.read_text(name) for name in REQUIRED_FILES}
        result['readback_passed'] = (run_capability_registry(contents) is None and
            all(contents[name] == (root / name).read_text(encoding='utf-8') for name in REQUIRED_FILES))
        result['artifact_sha256'] = {name: digest((root / name).read_bytes()) for name in REQUIRED_FILES}
    except Exception as exc:
        result['verification_error'] = f'{type(exc).__name__}: {exc}'
    result['conditions_unchanged'] = conditions_match(inputs, hashes)
    result['success'] = bool(accepted_result(result))
    result['first_attempt_success'] = result['success'] and measured.builder_starts == 1
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--series', required=True)
    parser.add_argument('--runs', type=int, choices=(1, 3), default=1)
    parser.add_argument('--accepted-first', type=Path)
    parser.add_argument('--initial-review', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9-]{1,40}', args.series):
        raise ValueError('invalid series name')
    inputs, hashes = frozen_inputs(), verifier_hashes()
    input_hashes = {name: digest(raw) for name, raw in inputs.items()}
    if args.runs == 3:
        if args.accepted_first is None or args.initial_review is None:
            raise ValueError('three repetitions require the independently accepted initial report')
        validate_initial_report(args.accepted_first, args.initial_review, inputs, hashes)
    elif args.accepted_first is not None or args.initial_review is not None:
        raise ValueError('accepted-first applies only to repetitions')
    root = ROOT / 'workspaces' / f'forge-registry-{args.series}'
    root.mkdir()
    spec = parse_task_spec((ROOT / 'examples/forge_capability_registry.json').read_text(encoding='utf-8'))
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'model': 'forge-30b', 'temperature': 0,
              'input_sha256': input_hashes, 'verifier_sha256': hashes, 'runs': []}
    for index in range(1, args.runs + 1):
        if verifier_hashes() != hashes or frozen_inputs() != inputs:
            raise ValueError('frozen conditions changed')
        print(f'RUN {index}/{args.runs}', flush=True)
        result = execute_run(root / f'run-{index}', spec, inputs, hashes,
                             LMStudioChatClient(model='forge-30b', timeout_seconds=300))
        report['runs'].append(result)
        report['conditions_unchanged'] = conditions_match(inputs, hashes)
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result), flush=True)
        if not report['conditions_unchanged']:
            raise ValueError('frozen conditions changed during build')
    return 0 if all(r['success'] for r in report['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
