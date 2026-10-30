"""Serial, independently gated four-build acceptance for validator/active stages."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.forge_runtime.lm_studio_executor import LMStudioChatClient
from src.forge_runtime.task_runner import parse_task_spec, run_forge_task, format_run_report
from src.forge_runtime.workspace_tools import WorkspaceFileTools
from src.forge_runtime.host_suites import run_host_suite
from scripts.verify_dialogue_host_gate import validate_runtime

ORIGIN = ROOT / 'workspaces/forge-registry-reviewer-repeat-20261003'
ORIGIN_REPORT_SHA256 = '66785149bb8c6eaede7437d38df1ce3ce86d520dfbc69d63f4ad6a66079301cd'
STAGES = {
    'validator': ('execution_contract', 'execution_contract_v1', 'run_execution_contract'),
    'active': ('active_execution', 'active_execution_v1', 'run_active_execution'),
}
REFERENCE_HASHES = {
    'validator': '2d33a0be22c4e6f1cf7b8ba06615282a5ab16a670cc7ad820ee33f6b8b244e4b',
    'active': '78147bbfe9228cdff642e75fdd04dbb7b38052af35fd7d99c26b288de6ad527d',
}


REFERENCE_PART_LINES = {
    'validator': ((1, 116), (117, 189), (190, 276), (277, 343), (344, 419), (420, 425)),
    'active': ((1, 89), (90, 178), (179, 254), (255, 337), (338, 367)),
}


def reference_parts(stage):
    """Freeze read-only inputs from unchanged original bytes; never write outputs."""
    validate_stage_reference(stage)
    original_name = 'execution_contract.py' if stage == 'validator' else 'active_scan.py'
    source = (ROOT / 'src/obsidian_policy' / original_name).read_bytes()
    if digest(source) != REFERENCE_HASHES[stage]:
        raise ValueError('original reference changed before fragment freezing')
    lines = source.splitlines(keepends=True)
    parts = {
        f'reference_parts/part-{index:02d}.txt': b''.join(lines[start-1:end])
        for index, (start, end) in enumerate(REFERENCE_PART_LINES[stage], 1)
    }
    if (b''.join(parts.values()) != source or
            any(not raw or len(raw.decode('utf-8')) > 3000 for raw in parts.values())):
        raise ValueError('fixed reference parts do not reproduce the complete original')
    return parts


REFERENCE_APPEND_MARKER = b'# FORGE_FRAGMENT_APPEND_20261003\n'


def reference_payloads(stage):
    """Add inert append metadata to read-only inputs, never to agent outputs."""
    parts = reference_parts(stage)
    if any(REFERENCE_APPEND_MARKER in raw for raw in parts.values()):
        raise ValueError('append marker collides with original reference source')
    return {
        f'reference_payloads/part-{index:02d}.txt':
            raw + (REFERENCE_APPEND_MARKER if index < len(parts) else b'')
        for index, raw in enumerate(parts.values(), 1)
    }


def validate_stage_reference(stage):
    """Reject source/spec drift against the reference accepted before models."""
    name = STAGES[stage][0]
    reference = importlib.import_module('src.forge_runtime.' + name + '_contract')
    expected = REFERENCE_HASHES[stage]
    original_name = 'execution_contract.py' if stage == 'validator' else 'active_scan.py'
    original = (ROOT / 'src/obsidian_policy' / original_name).read_bytes()
    specification = (ROOT / ('examples/forge_' + name + '_SPEC.md')).read_text(encoding='utf-8')
    blocks = re.findall(r'```python\n(.*?)\n```', specification, flags=re.DOTALL)
    if (reference.REFERENCE_SHA256 != expected or digest(original) != expected
            or digest(reference.EXPECTED_SOURCE.encode('utf-8')) != expected
            or blocks != [reference.EXPECTED_SOURCE]
            or specification.count('The original source bytehash is ' + expected + '.') != 1):
        raise ValueError('original source, frozen reference or specification changed')


def complete_report(report, stage, count, inputs, hashes):
    if (report.get('stage') != stage or report.get('model') != 'forge-30b'
            or report.get('temperature') != 0 or report.get('conditions_unchanged') is not True
            or len(report.get('runs', [])) != count
            or report.get('verifier_sha256') != hashes
            or report.get('input_sha256') != {p: digest(b) for p, b in inputs.items()}):
        raise ValueError('report has incomplete or inconsistent frozen conditions')
    validate_runtime(report.get('runtime_before'))
    validate_runtime(report.get('runtime_after'))
    paths = [workspace_path(run['workspace']) for run in report['runs']]
    if len(set(paths)) != count:
        raise ValueError('report does not describe unique fresh workspaces')


def valid_reviewer(value):
    return type(value) is str and bool(value.strip())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def stage_suite(stage):
    name, identifier, runner = STAGES[stage]
    module = importlib.import_module('src.forge_runtime.' + name + '_suite')
    return module.REQUIRED_FILES, identifier, getattr(module, runner)


def verifier_hashes(stage):
    paths = list((ROOT / 'src').rglob('*.py'))
    paths += [Path(__file__), ROOT / 'scripts/verify_dialogue_host_gate.py',
              ROOT / 'FORGE_EXECUTION_CONTRACT_REPRODUCTION_CONTRACT.md']
    name = STAGES[stage][0]
    paths += [ROOT / ('examples/forge_' + name + '.json'),
              ROOT / ('examples/forge_' + name + '_SPEC.md')]
    paths += sorted((ROOT / 'tests').glob('test_forge_*execution*.py'))
    paths += sorted((ROOT / 'tests').glob('test_obsidian_issued*.py'))
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted(set(paths))}


def evidence_run(root, result, required, runner):
    if not accepted_result(result, required):
        raise ValueError('prior build is not fully accepted')
    actual = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    if actual != result:
        raise ValueError('stored result differs from report')
    for name, expected in result['artifact_sha256'].items():
        if digest((root / name).read_bytes()) != expected:
            raise ValueError('prior artifact changed: ' + name)
    for name in ('model_calls.jsonl', 'trace.jsonl'):
        if digest((root / name).read_bytes()) != result[name + '_sha256']:
            raise ValueError('prior raw evidence changed')
    if runner({name: (root / name).read_text(encoding='utf-8') for name in required}) is not None:
        raise ValueError('prior actual modules fail current fixed suite')


def frozen_inputs(stage, validator_report=None, validator_review=None):
    validate_stage_reference(stage)
    from src.forge_runtime.capability_registry_suite import REQUIRED_FILES as registry_files
    origin_raw = (ORIGIN / 'report.json').read_bytes()
    if digest(origin_raw) != ORIGIN_REPORT_SHA256:
        raise ValueError('externally accepted registry report changed')
    accepted = json.loads(origin_raw)
    if len(accepted['runs']) != 3 or not all(run['success'] is True for run in accepted['runs']):
        raise ValueError('accepted registry origin lacks the three positive repeats')
    result = accepted['runs'][0]
    origin = ORIGIN / 'run-1'
    if json.loads((origin / 'result.json').read_text(encoding='utf-8')) != result:
        raise ValueError('registry origin result mismatch')
    inputs = {}
    for name in registry_files:
        raw = (origin / name).read_bytes()
        if digest(raw) != result['artifact_sha256'][name]:
            raise ValueError('accepted registry input changed: ' + name)
        inputs[name] = raw
    name = STAGES[stage][0]
    inputs['SPEC.md'] = (ROOT / ('examples/forge_' + name + '_SPEC.md')).read_bytes()
    inputs.update(reference_parts(stage))
    inputs.update(reference_payloads(stage))
    if stage == 'active':
        if validator_report is None or validator_review is None:
            raise ValueError('active stage needs independently accepted validator series')
        report_path = workspace_path(validator_report)
        review_path = workspace_path(validator_review)
        report_raw = report_path.read_bytes()
        report = json.loads(report_raw)
        review = json.loads(review_path.read_text(encoding='utf-8'))
        if (review.get('kind') != 'forge_execution_series_review' or review.get('stage') != 'validator'
                or review.get('approved') is not True or not valid_reviewer(review.get('reviewer'))
                or review.get('report_sha256') != digest(report_raw)
                or report.get('stage') != 'validator' or len(report.get('runs', [])) != 3):
            raise ValueError('validator series independent review missing or unbound')
        required, _, runner = stage_suite('validator')
        validator_inputs = frozen_inputs('validator')
        complete_report(report, 'validator', 3, validator_inputs, verifier_hashes('validator'))
        paths = [workspace_path(result['workspace']) for result in report['runs']]
        if len(set(paths)) != 3:
            raise ValueError('validator repeats must be three unique fresh workspaces')
        chain = report.get('initial_acceptance', {})
        if set(chain) != {'report', 'review', 'report_sha256', 'review_sha256'}:
            raise ValueError('validator initial plus repeat acceptance chain missing')
        initial, initial_review = workspace_path(chain['report']), workspace_path(chain['review'])
        if (digest(initial.read_bytes()) != chain['report_sha256']
                or digest(initial_review.read_bytes()) != chain['review_sha256']):
            raise ValueError('validator initial acceptance chain changed')
        validate_initial_report('validator', initial, initial_review,
                                validator_inputs, report['verifier_sha256'])
        first = json.loads(initial.read_text(encoding='utf-8'))['runs'][0]
        if workspace_path(first['workspace']) in paths:
            raise ValueError('initial build reused as a repetition')
        for result in report['runs']:
            run = workspace_path(result['workspace'])
            run.relative_to(report_path.parent)
            evidence_run(run, result, required, runner)
            if any((run / p).read_bytes() != b for p, b in validator_inputs.items()):
                raise ValueError('actual validator repetition input changed')
        selected = workspace_path(report['runs'][0]['workspace'])
        inputs['agent/execution_contract.py'] = (selected / 'agent/execution_contract.py').read_bytes()
    return inputs


def workspace_path(value):
    from src.forge_runtime.project_context import _reject_links
    path = Path(value)
    path = (ROOT / path if not path.is_absolute() else path).absolute()
    _reject_links(path)
    resolved = path.resolve()
    resolved.relative_to((ROOT / 'workspaces').resolve())
    return resolved


def accepted_result(result, required):
    pipeline = result.get('pipeline', {})
    roles = pipeline.get('results', [])
    return (all(result.get(key) is True for key in ('approved', 'independent_passed',
             'inputs_unchanged', 'readback_passed', 'conditions_unchanged'))
            and pipeline.get('approved') is True
            and [r.get('role') for r in roles] == ['builder', 'tester', 'reviewer']
            and all(r.get('passed') is True for r in roles)
            and type(result.get('artifact_sha256')) is dict
            and set(result['artifact_sha256']) == set(required)
            and all(type(v) is str and re.fullmatch('[0-9a-f]{64}', v)
                    for v in result['artifact_sha256'].values())
            and all(type(result.get(n + '_sha256')) is str and
                    re.fullmatch('[0-9a-f]{64}', result[n + '_sha256'])
                    for n in ('model_calls.jsonl', 'trace.jsonl')))


class RecordedChat:
    def __init__(self, client, directory):
        self.client, self.directory = client, directory
        self.lock, self.closed = threading.Lock(), False
        self.model_calls = self.builder_starts = self.repair_actions = 0
        self.pending = {}

    def set_tool_actions(self, actions):
        self.client.set_tool_actions(actions)

    def _write(self, raw, trace):
        # Caller holds the lock; close and callback completion cannot interleave.
        for filename, entry in (('model_calls.jsonl', raw), ('trace.jsonl', trace)):
            with (self.directory / filename).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(entry, ensure_ascii=True) + '\n')

    def __call__(self, messages):
        role = json.loads(messages[1][1])['role']
        with self.lock:
            if self.closed:
                raise ValueError('recorder finalized')
            self.model_calls += 1
            call_id = self.model_calls
            started = time.monotonic()
            raw = {'call_id': call_id, 'messages': messages,
                   'started_utc': datetime.now(timezone.utc).isoformat()}
            trace = {'call_id': call_id, 'role': role, 'last_input': messages[-1][1]}
            self.pending[call_id] = (raw, trace, started)
            if role == 'builder' and len(messages) in (2, 3):
                if len(messages) == 2 or json.loads(messages[2][1]).get('kind') == 'host_check_feedback':
                    self.builder_starts += 1
        response = error = None
        action = None
        try:
            response = self.client(messages)
            action = json.loads(response).get('action')
            print(f'  {role}: {action}', flush=True)
            return response
        except BaseException as exc:
            error = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            with self.lock:
                if not self.closed:
                    if response is not None:
                        raw['response'] = trace['response'] = response
                    if error is not None:
                        raw['error'] = trace['error'] = error
                    raw['callback_complete'] = trace['callback_complete'] = True
                    trace['seconds'] = round(time.monotonic() - started, 3)
                    if action in ('replace_text', 'replace_text_batch'):
                        self.repair_actions += 1
                    self._write(raw, trace)
                    del self.pending[call_id]

    def close(self, reason=None):
        with self.lock:
            if self.closed:
                return
            for raw, trace, started in self.pending.values():
                raw['callback_complete'] = trace['callback_complete'] = False
                raw['finalization_reason'] = trace['finalization_reason'] = (
                    reason or 'recorder finalized before callback completed')
                trace['seconds'] = round(time.monotonic() - started, 3)
                self._write(raw, trace)
            self.pending.clear()
            self.closed = True


def conditions_match(stage, inputs, hashes, validator_report=None, validator_review=None):
    try:
        return verifier_hashes(stage) == hashes and frozen_inputs(stage, validator_report, validator_review) == inputs
    except Exception:
        return False


def validate_initial_report(stage, report_path, review_path, inputs, hashes):
    report_path, review_path = workspace_path(report_path), workspace_path(review_path)
    raw = report_path.read_bytes()
    report = json.loads(raw)
    review = json.loads(review_path.read_text(encoding='utf-8'))
    required, _, runner = stage_suite(stage)
    complete_report(report, stage, 1, inputs, hashes)
    if (report.get('stage') != stage or report.get('verifier_sha256') != hashes
            or report.get('input_sha256') != {p: digest(b) for p, b in inputs.items()}
            or report.get('model') != 'forge-30b' or report.get('temperature') != 0
            or len(report.get('runs', [])) != 1):
        raise ValueError('initial evidence or frozen conditions mismatch')
    result = report['runs'][0]
    run = workspace_path(result['workspace'])
    run.relative_to(report_path.parent)
    if (set(review) != {'kind', 'stage', 'approved', 'reviewer', 'report_sha256',
                       'result_sha256', 'model_calls_sha256'}
            or review['kind'] != 'forge_execution_initial_review' or review['stage'] != stage
            or review['approved'] is not True or not valid_reviewer(review['reviewer'])
            or review['report_sha256'] != digest(raw)
            or review['result_sha256'] != digest((run / 'result.json').read_bytes())
            or review['model_calls_sha256'] != digest((run / 'model_calls.jsonl').read_bytes())):
        raise ValueError('independent initial review missing or unbound')
    evidence_run(run, result, required, runner)
    if any((run / p).read_bytes() != b for p, b in inputs.items()):
        raise ValueError('actual initial input changed')
    return True


def runtime_snapshot():
    result = subprocess.run(['lms', 'ps', '--json'], capture_output=True, text=True,
                            encoding='utf-8', timeout=30, check=True)
    snapshot = json.loads(result.stdout)
    validate_runtime(snapshot)
    return snapshot


def execute_run(root, stage, spec, inputs, hashes, client, validator_report=None, validator_review=None):
    root.mkdir()
    for name, raw in inputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if path.read_bytes() != raw:
            raise ValueError('input copy readback mismatch')
    chat = RecordedChat(client, root)
    started = time.monotonic()
    result = {'workspace': root.relative_to(ROOT).as_posix(), 'approved': False,
              'independent_passed': False, 'readback_passed': False, 'manual_interventions': 0}
    try:
        pipeline = run_forge_task(root, spec, chat, max_steps=20, max_call_seconds=300,
                                  max_elapsed_seconds=900, max_repair_attempts=1,
                                  execution_copy_stage=stage)
        result['pipeline'] = json.loads(format_run_report(pipeline))
        result['approved'] = pipeline.approved
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        chat.close(result.get('error'))
    result.update(build_seconds=round(time.monotonic() - started, 3), model_calls=chat.model_calls,
                  repair_loops=max(0, chat.builder_starts - 1), repair_actions=chat.repair_actions)
    result['inputs_unchanged'] = all((root / p).is_file() and (root / p).read_bytes() == b for p, b in inputs.items())
    required, identifier, runner = stage_suite(stage)
    try:
        workspace = WorkspaceFileTools(root, spec.task)
        result['independent_passed'], result['independent_detail'] = run_host_suite(identifier, workspace)
        contents = {p: workspace.read_text(p) for p in required}
        result['readback_passed'] = (runner(contents) is None and
                                   all(contents[p] == (root / p).read_text(encoding='utf-8') for p in required))
        result['artifact_sha256'] = {p: digest((root / p).read_bytes()) for p in required}
    except Exception as exc:
        result['verification_error'] = f'{type(exc).__name__}: {exc}'
    for name in ('model_calls.jsonl', 'trace.jsonl'):
        path = root / name
        result[name + '_sha256'] = digest(path.read_bytes()) if path.is_file() else None
    result['conditions_unchanged'] = conditions_match(stage, inputs, hashes, validator_report, validator_review)
    result['success'] = bool(accepted_result(result, required))
    result['first_attempt_success'] = result['success'] and chat.builder_starts == 1
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    if json.loads((root / 'result.json').read_text(encoding='utf-8')) != result:
        raise ValueError('result readback mismatch')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=tuple(STAGES), required=True)
    parser.add_argument('--series', required=True)
    parser.add_argument('--runs', type=int, choices=(1, 3), default=1)
    parser.add_argument('--accepted-first', type=Path)
    parser.add_argument('--initial-review', type=Path)
    parser.add_argument('--accepted-validator', type=Path)
    parser.add_argument('--validator-review', type=Path)
    args = parser.parse_args()
    if not re.fullmatch('[a-z0-9-]{1,40}', args.series):
        raise ValueError('invalid series name')
    inputs = frozen_inputs(args.stage, args.accepted_validator, args.validator_review)
    hashes = verifier_hashes(args.stage)
    if args.runs == 3:
        if args.accepted_first is None or args.initial_review is None:
            raise ValueError('three fresh repetitions require independent initial acceptance')
        validate_initial_report(args.stage, args.accepted_first, args.initial_review, inputs, hashes)
    elif args.accepted_first is not None or args.initial_review is not None:
        raise ValueError('accepted-first applies only to repetitions')
    root = ROOT / 'workspaces' / ('forge-' + STAGES[args.stage][0] + '-' + args.series)
    from src.forge_runtime.project_context import _reject_links
    _reject_links(root)
    root.mkdir()
    name = STAGES[args.stage][0]
    spec = parse_task_spec((ROOT / ('examples/forge_' + name + '.json')).read_text(encoding='utf-8'))
    report = {'stage': args.stage, 'model': 'forge-30b', 'temperature': 0,
              'started_utc': datetime.now(timezone.utc).isoformat(),
              'input_sha256': {p: digest(b) for p, b in inputs.items()}, 'verifier_sha256': hashes,
              'runs': []}
    if args.runs == 3:
        report['initial_acceptance'] = {
            'report': workspace_path(args.accepted_first).relative_to(ROOT).as_posix(),
            'review': workspace_path(args.initial_review).relative_to(ROOT).as_posix(),
            'report_sha256': digest(workspace_path(args.accepted_first).read_bytes()),
            'review_sha256': digest(workspace_path(args.initial_review).read_bytes()),
        }
    (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    report['runtime_before'] = runtime_snapshot()
    for index in range(1, args.runs + 1):
        if not conditions_match(args.stage, inputs, hashes, args.accepted_validator, args.validator_review):
            raise ValueError('frozen conditions changed')
        print(f'RUN {index}/{args.runs}', flush=True)
        result = execute_run(root / f'run-{index}', args.stage, spec, inputs, hashes,
                             LMStudioChatClient(model='forge-30b', timeout_seconds=300,
                                                execution_copy_field_order=True),
                             args.accepted_validator, args.validator_review)
        report['runs'].append(result)
        report['conditions_unchanged'] = conditions_match(args.stage, inputs, hashes,
                                                        args.accepted_validator, args.validator_review)
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result), flush=True)
        if not report['conditions_unchanged']:
            raise ValueError('frozen conditions changed during build')
    report['runtime_after'] = runtime_snapshot()
    (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0 if all(r['success'] for r in report['runs']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
