import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeRunCommandTest(unittest.TestCase):
    def test_run_uses_loaded_model_and_restricted_cli(self):
        manager = (ROOT / "scripts" / "manage_agent1.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('"Run"', manager)
        self.assertIn("Get-AgentModelIdentifier", manager)
        self.assertIn("$_.modelKey -eq $modelKey", manager)
        self.assertIn("--task-file $resolvedTaskFile", manager)
        self.assertIn("--workspace $resolvedWorkspace", manager)
        self.assertIn("--model $script:activeModelIdentifier", manager)
        self.assertIn("python -m src.forge_runtime.cli", manager)


if __name__ == "__main__":
    unittest.main()
