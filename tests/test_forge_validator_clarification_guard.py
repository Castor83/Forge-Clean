"""Preparation guard rejects mutations without making a model/HTTP request."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_validator_clarification as prep


class ClarificationGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / 'preparation'
        self.directory.mkdir()
        self.request = prep.encode({'model': 'forge-30b', 'temperature': 0,
                                    'messages': [{'role': 'user', 'content': 'fixed requirements'}]})
        self.sources = ['task.json', 'schema.py', 'host.py', 'reference.txt', 'config.json']
        self.inventory = list(self.sources)
        self.artifacts = ['request.json', 'payload.txt', 'prompt.diff']
        for name in self.sources:
            (self.root / name).write_bytes(('fixed '+name).encode())
        for name in self.artifacts:
            (self.directory / name).write_bytes(self.request if name == 'request.json' else b'fixed')
        self.frozen = {'configuration': copy.deepcopy(prep.CONFIG),
                       'verifier_paths': sorted(self.inventory),
                       'source_sha256': {n: prep.digest((self.root/n).read_bytes()) for n in self.sources},
                       'artifact_sha256': {n: prep.digest((self.directory/n).read_bytes()) for n in self.artifacts}}
        raw = prep.encode(self.frozen)
        (self.directory/'freeze.json').write_bytes(raw)
        self.pin = prep.digest(raw)
        root_patch = patch.object(prep, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        offline_patch = patch.object(prep, 'prepared_request', return_value=((), (), self.request, b'fixed'))
        offline_patch.start()
        self.addCleanup(offline_patch.stop)
        inventory_patch = patch.object(prep.benchmark, 'verifier_hashes',
                                       side_effect=lambda stage: dict.fromkeys(self.inventory, 'fixture'))
        inventory_patch.start()
        self.addCleanup(inventory_patch.stop)

    def test_identical_preparation_and_candidate_pass(self):
        self.assertTrue(prep.verify_preparation(self.directory, self.pin, self.request,
                                               copy.deepcopy(prep.CONFIG)))

    def test_each_bound_source_change_is_rejected(self):
        for name in self.sources:
            with self.subTest(source=name):
                path = self.root/name
                original = path.read_bytes()
                path.write_bytes(original+b'changed')
                with self.assertRaisesRegex(ValueError, 'bound source changed'):
                    prep.verify_preparation(self.directory, self.pin)
                path.write_bytes(original)
        self.inventory.append('additional_host.py')
        with self.assertRaisesRegex(ValueError, 'verifier file inventory changed'):
            prep.verify_preparation(self.directory, self.pin)

    def test_each_prepared_artifact_change_is_rejected(self):
        for name in self.artifacts:
            with self.subTest(artifact=name):
                path = self.directory/name
                original = path.read_bytes()
                path.write_bytes(original+b'changed')
                with self.assertRaisesRegex(ValueError, 'prepared artifact changed'):
                    prep.verify_preparation(self.directory, self.pin)
                path.write_bytes(original)

    def test_rewritten_freeze_and_recomputed_internal_hash_are_rejected(self):
        (self.directory/'payload.txt').write_bytes(b'replaced payload')
        changed = copy.deepcopy(self.frozen)
        changed['artifact_sha256']['payload.txt'] = prep.digest(b'replaced payload')
        (self.directory/'freeze.json').write_bytes(prep.encode(changed))
        with self.assertRaisesRegex(ValueError, 'freeze binding changed'):
            prep.verify_preparation(self.directory, self.pin)

    def test_changed_candidate_request_and_config_are_rejected(self):
        candidate = json.loads(self.request)
        candidate['messages'][0]['content'] = 'additional prompt change'
        with self.assertRaisesRegex(ValueError, 'candidate request differs'):
            prep.verify_preparation(self.directory, self.pin, prep.encode(candidate))
        for name in prep.CONFIG:
            with self.subTest(configuration=name):
                changed = copy.deepcopy(prep.CONFIG)
                changed[name] = 'different'
                with self.assertRaisesRegex(ValueError, 'configuration changed'):
                    prep.verify_preparation(self.directory, self.pin, self.request, changed)


if __name__ == '__main__':
    unittest.main()
