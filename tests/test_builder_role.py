import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BuilderRoleTest(unittest.TestCase):
    def test_agent_has_general_builder_role(self) -> None:
        rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Du bist **Forge**", rules)
        self.assertIn("Entwicklungs- und Bau-Agent", rules)
        self.assertIn("Obsidian", rules)
        self.assertIn("überprüfbare Erfolgskriterien", rules)
        self.assertIn("eng begrenzten", rules)
        self.assertIn("SECURITY_STATUS.md", rules)
        self.assertIn("ausschließlich den aktuellen Benutzerauftrag", rules)

    def test_existing_chat_has_explicit_activation_prompt(self) -> None:
        prompt = (ROOT / "BUILDER_ACTIVATION_PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("**Forge**", prompt)
        self.assertIn("**Obsidian**", prompt)
        self.assertIn("Warte nach der Bestätigung", prompt)
        self.assertIn("`Pause`", prompt)

    def test_first_run_uses_builder_role(self) -> None:
        prompt = (ROOT / "FIRST_RUN_PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("**Forge**", prompt)
        self.assertIn("noch kein Bauauftrag", prompt)

    def test_forge_goal_and_first_task_focus_on_builder_readiness(self) -> None:
        goal = (ROOT / "FORGE_GOAL.md").read_text(encoding="utf-8")
        task = (ROOT / "FIRST_FORGE_TASK.md").read_text(encoding="utf-8")
        normalized_goal = " ".join(goal.split())
        self.assertIn(
            "zuerst die Baufähigkeiten, danach der weitere Ausbau "
            "der Gesprächs- und Sprachoberfläche",
            normalized_goal,
        )
        self.assertIn(
            "Bestehende Sicherheitsgrenzen und Hostprüfungen bleiben verbindlich.",
            normalized_goal,
        )
        self.assertIn(
            "allgemeine Bauzuverlässigkeit ist noch nicht nachgewiesen.",
            normalized_goal,
        )
        self.assertIn("SECURITY_STATUS.md", goal)
        self.assertIn("Obsidian ist noch kein Bauauftrag", task)
        self.assertIn("`tests/test_health.py`", task)
        self.assertIn("Aktualisiere `PAUSE_STATE.md` nicht", task)


if __name__ == "__main__":
    unittest.main()
