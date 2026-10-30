import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.forge_runtime import parse_task_spec, run_forge_task


def _spec():
    return parse_task_spec(
        json.dumps(
            {
                "task_id": "preflight-budget",
                "objective": "Write the required final marker",
                "allowed_files": ["result.txt"],
                "checks": [
                    {
                        "kind": "contains_text",
                        "path": "result.txt",
                        "text": "FINAL",
                    }
                ],
            }
        )
    )


def _finish(summary, passed):
    return json.dumps(
        {
            "action": "finish",
            "passed": passed,
            "summary": summary,
            "findings": [],
        }
    )


class RecordingChat:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return next(self.responses)


class TestForgePreflightRepairBudget(unittest.TestCase):
    def test_preflight_failure_consumes_the_single_repair_attempt(self):
        chat = RecordingChat(
            (
                json.dumps(
                    {
                        "action": "replace_text_batch",
                        "path": "result.txt",
                        "replacements": [
                            {"old": "DRAFT", "new": "STILL_BAD"}
                        ],
                    }
                ),
                json.dumps({"action": "run_check"}),
                _finish("repair still fails", passed=False),
                json.dumps(
                    {
                        "action": "replace_text_batch",
                        "path": "result.txt",
                        "replacements": [
                            {"old": "STILL_BAD", "new": "FINAL"}
                        ],
                    }
                ),
                json.dumps({"action": "run_check"}),
                _finish("unbudgeted second repair", passed=True),
            )
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.txt").write_text("DRAFT", encoding="utf-8")

            result = run_forge_task(
                root,
                _spec(),
                chat,
                max_repair_attempts=1,
            )

            self.assertFalse(result.approved)
            self.assertEqual(
                (root / "result.txt").read_text(encoding="utf-8"),
                "STILL_BAD",
            )

        self.assertEqual(len(chat.calls), 3)
        feedback = json.loads(chat.calls[0][2][1])
        self.assertEqual(feedback["kind"], "host_check_feedback")
        self.assertIn("required text is missing: FINAL", feedback["output"])


if __name__ == "__main__":
    unittest.main()
