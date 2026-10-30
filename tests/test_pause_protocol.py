import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PauseProtocolTest(unittest.TestCase):
    def test_rules_require_an_explicit_standalone_pause_instruction(self) -> None:
        rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("eigenständige Arbeitsanweisung ausdrücklich `Pause`", rules)
        self.assertIn("`PAUSE_STATE.md` unverändert", rules)

    def test_updater_refuses_to_run_without_explicit_pause_confirmation(self) -> None:
        script = ROOT / "scripts" / "update_pause_state.ps1"
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-CurrentGoal",
                "Testziel",
                "-CompletedWork",
                "Testarbeit",
                "-ChangedFiles",
                "Keine",
                "-Decisions",
                "Keine",
                "-NextStep",
                "Weiter testen",
                "-Tests",
                "Nicht ausgeführt",
                "-Risks",
                "Keine",
                "-Processes",
                "Keine",
                "-ResumeNotes",
                "Testhinweis",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PAUSE_STATE.md darf nur", result.stderr + result.stdout)
        self.assertIn("'Pause'", result.stderr + result.stdout)

    def test_documented_example_includes_explicit_pause_confirmation(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("-ExplicitPause", readme)


if __name__ == "__main__":
    unittest.main()
