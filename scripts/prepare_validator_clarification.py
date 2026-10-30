"""Offline-only preparation and fail-closed evidence guard; no model transport."""
import argparse
import difflib
import hashlib
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import benchmark_execution_contract as benchmark
from src.forge_runtime.lm_studio_executor import LMStudioChatClient

BASELINE = 'workspaces/forge-execution_contract-initial-fieldorder-20261003/run-1/model_calls.jsonl'
BASELINE_SHA = '883e42615d7a831b5f57127b8c6e0461899b76dff4f3ccaf1267a9a85bdd03d9'
CLARIFICATION = (
    'old and new are independent strings and may contain the same marker. '
    'Do not subtract old from the content. This non-final payload intentionally '
    'contains the marker at its end: preserve it in new, even though old also '
    'contains it. Preserve every character and trailing LF. '
    'Do not execute, summarize or clean up text. '
)
CONFIG = {'model': 'forge-30b', 'temperature': 0, 'context': 32768, 'parallel': 1,
          'timeout_seconds': 300, 'transport_attempts_max': 1, 'model_calls_max': 1,
          'endpoint': 'http://127.0.0.1:1234/v1/chat/completions',
          'execution_copy_field_order': True, 'actions': ['replace_text', 'finish']}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2)+'\n').encode('utf-8')


def prepared_request():
    raw = (ROOT / BASELINE).read_bytes()
    if digest(raw) != BASELINE_SHA:
        raise ValueError('historical baseline changed')
    row = next(json.loads(line) for line in raw.decode('utf-8').splitlines()
               if json.loads(line)['call_id'] == 5)
    original = tuple(tuple(message) for message in row['messages'])
    if [m[0] for m in original] != ['system', 'user', 'user']:
        raise ValueError('historical message layout changed')
    messages = ((original[0][0], original[0][1]+'\n'+CLARIFICATION),)+original[1:]
    captured = []
    def offline_opener(request, timeout):
        captured.append(request.data)
        # Only serialization is exercised. No socket or real HTTP transport exists here.
        return io.BytesIO(b'{"choices":[{"message":{"content":"PREPARED_ONLY"}}]}')
    client = LMStudioChatClient(CONFIG['model'], endpoint=CONFIG['endpoint'],
                               timeout_seconds=CONFIG['timeout_seconds'],
                               opener=offline_opener, execution_copy_field_order=True)
    client.set_tool_actions(tuple(CONFIG['actions']))
    client(messages)
    if len(captured) != 1:
        raise ValueError('offline serialization count mismatch')
    payload = benchmark.reference_payloads('validator')['reference_payloads/part-02.txt']
    if json.loads(original[2][1])['content'].encode('utf-8') != payload:
        raise ValueError('historical payload differs from frozen original')
    return original, messages, captured[0], payload


def verify_preparation(directory, expected_freeze_sha256, candidate_request=None,
                       candidate_configuration=None):
    raw = (directory / 'freeze.json').read_bytes()
    if digest(raw) != expected_freeze_sha256:
        raise ValueError('freeze binding changed')
    frozen = json.loads(raw)
    if sorted(benchmark.verifier_hashes('validator')) != frozen['verifier_paths']:
        raise ValueError('verifier file inventory changed')
    for name, expected in frozen['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('bound source changed: '+name)
    for name, expected in frozen['artifact_sha256'].items():
        if digest((directory / name).read_bytes()) != expected:
            raise ValueError('prepared artifact changed: '+name)
    _, _, current_request, _ = prepared_request()
    saved_request = (directory / 'request.json').read_bytes()
    if current_request != saved_request:
        raise ValueError('current request differs from preparation')
    if candidate_request is not None and candidate_request != saved_request:
        raise ValueError('candidate request differs from preparation')
    configuration = CONFIG if candidate_configuration is None else candidate_configuration
    if encode(configuration) != encode(frozen['configuration']):
        raise ValueError('configuration changed')
    return True


def prepare(directory):
    old_freeze = json.loads((ROOT / 'workspaces/execution-field-order-freeze-20261003.json').read_text())
    source_hashes = benchmark.verifier_hashes('validator')
    verifier_paths = sorted(source_hashes)
    if source_hashes != old_freeze['verifier_sha256']['validator']:
        raise ValueError('historical validator conditions changed')
    original, messages, request, payload = prepared_request()
    artifacts = {'request.json': request, 'payload.txt': payload,
                 'original-messages.json': encode(original), 'prepared-messages.json': encode(messages),
                 'prompt.diff': ''.join(difflib.unified_diff((original[0][1]+'\n').splitlines(True),
                     (messages[0][1]+'\n').splitlines(True), fromfile='historical-system',
                     tofile='clarified-system')).encode('utf-8')}
    for name in [BASELINE, 'scripts/prepare_validator_clarification.py',
                 'tests/test_forge_validator_clarification_guard.py',
                 'AGENTS.md', 'FORGE_GOAL.md', 'SECURITY_STATUS.md', 'OBSIDIAN_SPEC.md',
                 'workspaces/execution-field-order-freeze-20261003.json']:
        source_hashes[name] = digest((ROOT / name).read_bytes())
    directory.mkdir()  # Never reuse an existing preparation or overwrite old evidence.
    for name, raw in artifacts.items():
        (directory / name).write_bytes(raw)
        if (directory / name).read_bytes() != raw:
            raise ValueError('artifact readback mismatch')
    frozen = {'kind': 'prepared_only_no_model', 'configuration': CONFIG,
              'verifier_paths': verifier_paths,
              'source_sha256': source_hashes,
              'artifact_sha256': {n: digest(v) for n, v in artifacts.items()},
              'baseline_call': 5, 'only_prompt_change': CLARIFICATION}
    frozen_raw = encode(frozen)
    (directory / 'freeze.json').write_bytes(frozen_raw)
    pin = digest(frozen_raw)
    verify_preparation(directory, pin)
    print('PREPARED_ONLY model_calls=0 transport_attempts=0 FREEZE_SHA256='+pin)
    return pin


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true', required=True)
    parser.parse_args()
    prepare(ROOT / 'workspaces/validator-clarification-preparation-v2-20261004')
