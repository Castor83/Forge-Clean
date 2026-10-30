import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeConfigTest(unittest.TestCase):
    def test_forge_model_uses_32k_context_and_single_parallel_request(self) -> None:
        manager = (ROOT / "scripts" / "manage_agent1.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("[int]$ContextLength = 32768", manager)
        self.assertIn("--parallel 1", manager)
        self.assertIn('$modelIdentifier = "forge-30b"', manager)


if __name__ == "__main__":
    unittest.main()
