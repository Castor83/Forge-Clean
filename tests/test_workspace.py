import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent1_workspace import WORKSPACE_NAME


class WorkspaceSmokeTest(unittest.TestCase):
    def test_workspace_identity(self) -> None:
        self.assertEqual(WORKSPACE_NAME, "Agent 1")

    def test_agent_rules_exist(self) -> None:
        self.assertTrue((ROOT / "AGENTS.md").is_file())

    def test_pause_protocol_exists(self) -> None:
        state = ROOT / "PAUSE_STATE.md"
        updater = ROOT / "scripts" / "update_pause_state.ps1"
        self.assertTrue(state.is_file())
        self.assertTrue(updater.is_file())
        text = state.read_text(encoding="utf-8")
        self.assertIn("<!-- PAUSE_NEXT_STEP -->", text)
        self.assertIn("<!-- PAUSE_RUNNING_PROCESSES -->", text)

    def test_bionic_setup_exists(self) -> None:
        setup = (ROOT / "BIONIC_SETUP.md").read_text(encoding="utf-8")
        prompt = (ROOT / "FIRST_RUN_PROMPT.md").read_text(encoding="utf-8")
        self.assertIn(r"V:\KI-Agenten\Agent-1-Workspace", setup)
        self.assertIn("AGENTS.md", prompt)
        self.assertIn("PAUSE_STATE.md", prompt)

    def test_runtime_is_local_only(self) -> None:
        manager = (ROOT / "scripts" / "manage_agent1.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("qwen3-coder-30b-a3b-instruct", manager)
        self.assertIn("--bind 127.0.0.1", manager)

    def test_bionic_uses_explicit_powershell_executable(self) -> None:
        rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        retry = (ROOT / "RETRY_PROMPT.md").read_text(encoding="utf-8")
        command = (
            "powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File ./scripts/verify.ps1"
        )
        self.assertIn(command, rules)
        self.assertIn(command, retry)


if __name__ == "__main__":
    unittest.main()
