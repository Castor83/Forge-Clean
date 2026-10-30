"""Series preflight refuses changed evidence before any model can run."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import benchmark_reminder_build as runner


class ReminderSeriesGuardsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.initial = self.root / 'workspaces/forge-reminder-first-build-20261004/result.json'
        self.initial.parent.mkdir(parents=True)
        self.task = {'task_id': 'fixed-test-task', 'objective': 'fixed requirements'}
        self.source_hashes = {'src/forge_runtime/task_runner.py': 'fixed-host',
                              'scripts/benchmark_reminder_build.py': 'old-runner'}
        self.baseline = {'task': self.task, 'hashes': self.source_hashes,
                         'success': True, 'first_attempt_success': True}
        self.accepted_bytes = json.dumps(self.baseline).encode('utf-8')
        self.initial.write_bytes(self.accepted_bytes)
        self.current = dict(self.source_hashes)
        self.current['scripts/benchmark_reminder_build.py'] = 'series-runner'
        # Model the fixed accepted-result hash for these isolated synthetic bytes.
        # Changed bytes still use SHA256 and cannot match that acceptance binding.
        def bound_digest(raw):
            if raw == self.accepted_bytes:
                return '98c066aaee507dc0ae1593e626ecf062950edb3c3180ee4bfeadb71a04a59e93'
            return hashlib.sha256(raw).hexdigest()
        for replacement in [patch.object(runner, 'ROOT', self.root),
                            patch.object(runner, 'digest', side_effect=bound_digest),
                            patch.object(runner, 'hashes', side_effect=lambda: dict(self.current)),
                            patch.object(runner, 'task_data', side_effect=lambda: copy.deepcopy(self.task))]:
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_unchanged_baseline_allows_only_measurement_script_change(self):
        runner.validate_repeat_baseline()
        self.assertEqual(self.initial.read_bytes(), self.accepted_bytes)
        self.assertFalse((self.root / 'workspaces/forge-reminder-repeat-1-20261004').exists())

    def test_changed_initial_result_is_rejected(self):
        altered = copy.deepcopy(self.baseline)
        altered['success'] = False
        self.initial.write_text(json.dumps(altered), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'accepted initial result changed'):
            runner.validate_repeat_baseline()

    def test_changed_task_is_rejected(self):
        self.task = dict(self.task, objective='different requirements')
        with self.assertRaisesRegex(ValueError, 'initial task or host/model source conditions changed'):
            runner.validate_repeat_baseline()

    def test_changed_host_hash_is_rejected(self):
        self.current['src/forge_runtime/task_runner.py'] = 'modified-host'
        with self.assertRaisesRegex(ValueError, 'initial task or host/model source conditions changed'):
            runner.validate_repeat_baseline()

    def test_added_host_source_is_rejected(self):
        self.current['src/forge_runtime/new_gate.py'] = 'new-source'
        with self.assertRaisesRegex(ValueError, 'initial task or host/model source conditions changed'):
            runner.validate_repeat_baseline()


if __name__ == '__main__':
    unittest.main()
