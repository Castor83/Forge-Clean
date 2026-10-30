import json
import tempfile
import unittest
from pathlib import Path

from src.forge_runtime.contracts import SubagentResult, SubagentTask
from src.forge_runtime.tool_loop import (
    ReplaceTextAction,
    ReplaceTextBatchAction,
    parse_tool_action,
    run_tool_loop,
)
from src.forge_runtime.workspace_tools import WorkspaceFileTools


class ScriptedChat:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return next(self.responses)


class ConfiguringChat(ScriptedChat):
    def __init__(self, responses):
        super().__init__(responses)
        self.tool_actions = None

    def set_tool_actions(self, actions):
        self.tool_actions = actions


def finish(passed=True):
    return json.dumps(
        {
            "action": "finish",
            "passed": passed,
            "summary": "done",
            "findings": [],
        }
    )


class TestForgeReplaceText(unittest.TestCase):
    def test_parses_only_bounded_exact_replacement_batches(self):
        content = json.dumps(
            {
                "action": "replace_text_batch",
                "path": "agent.py",
                "replacements": [
                    {"old": "before", "new": "middle"},
                    {"old": "middle", "new": "after"},
                ],
            }
        )
        self.assertEqual(
            parse_tool_action(content),
            ReplaceTextBatchAction(
                "agent.py",
                (("before", "middle"), ("middle", "after")),
            ),
        )
        sixteen = {
            "action": "replace_text_batch",
            "path": "agent.py",
            "replacements": [
                {"old": f"before-{index}", "new": f"after-{index}"}
                for index in range(16)
            ],
        }
        self.assertEqual(
            len(parse_tool_action(json.dumps(sixteen)).replacements),
            16,
        )
        invalid = (
            {
                "action": "replace_text_batch",
                "path": "agent.py",
                "replacements": [],
            },
            {
                "action": "replace_text_batch",
                "path": "agent.py",
                "replacements": [
                    {"old": str(index), "new": "x"} for index in range(17)
                ],
            },
            {
                "action": "replace_text_batch",
                "path": "agent.py",
                "replacements": [
                    {"old": "x", "new": "y"},
                    {"old": "z", "new": "z"},
                ],
            },
            {
                "action": "replace_text_batch",
                "path": "agent.py",
                "replacements": [
                    {"old": "x", "new": "y", "count": 1},
                    {"old": "z", "new": "q"},
                ],
            },
        )
        for action in invalid:
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    parse_tool_action(json.dumps(action))

    def test_existing_file_removes_write_file_from_model_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ConfiguringChat((finish(False),))

            run_tool_loop("builder", task, (), workspace, chat)

            self.assertEqual(
                chat.tool_actions,
                ("read_file", "replace_text", "finish"),
            )

    def test_missing_file_keeps_write_file_in_model_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ConfiguringChat((finish(False),))

            run_tool_loop("builder", task, (), workspace, chat)

            self.assertEqual(
                chat.tool_actions,
                ("read_file", "write_file", "replace_text", "finish"),
            )

    def test_repair_prompt_prefers_localized_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            specification = root / "SPEC.md"
            specification.write_text("large original specification", encoding="utf-8")
            task = SubagentTask(
                "replace",
                "read the complete original specification and rebuild everything",
                ("agent.py",),
                ("SPEC.md",),
            )
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat((finish(False),))

            run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                builder_repair_feedback="replace before with after",
            )

            system_prompt = chat.calls[0][0][1]
            self.assertIn("Read the affected file first", system_prompt)
            self.assertIn("Use one replace_text_batch", system_prompt)
            repair_handoff = json.loads(chat.calls[0][1][1])
            self.assertEqual(repair_handoff["role"], "builder")
            self.assertIn("only the host_check_feedback", repair_handoff["objective"])
            self.assertNotIn("read_only_files", repair_handoff)
            self.assertNotIn("original specification", chat.calls[0][1][1])

    def test_repair_exposes_batch_action_to_configured_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.py").write_text("one two", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ConfiguringChat((finish(False),))

            run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                builder_repair_feedback="fix two findings",
            )

            self.assertEqual(
                chat.tool_actions,
                (
                    "read_file",
                    "replace_text_batch",
                    "finish",
                ),
            )

    def test_repair_batch_applies_multiple_changes_in_one_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("one two three", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "one", "new": "ONE"},
                                {"old": "three", "new": "THREE"},
                            ],
                        }
                    ),
                    json.dumps({"action": "run_check"}),
                    finish(),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                check_runner=lambda: (True, "fixed"),
                builder_repair_feedback="fix both markers",
            )

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "ONE two THREE")
            self.assertEqual(
                json.loads(chat.calls[1][-1][1]),
                {
                    "path": "agent.py",
                    "replacements": 2,
                    "status": "batch_replaced",
                },
            )

    def test_invalid_repair_batch_leaves_file_completely_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("one two", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "one", "new": "ONE"},
                                {"old": "missing", "new": "fixed"},
                            ],
                        }
                    ),
                    finish(False),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                builder_repair_feedback="fix both markers",
            )

            self.assertFalse(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "one two")
            feedback = json.loads(chat.calls[1][-1][1])
            self.assertEqual(feedback["error"], "batch_old_text_occurrence_count")
            self.assertEqual(feedback["index"], 1)

    def test_replacement_batch_is_repair_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("one two", encoding="utf-8")
            task = SubagentTask("replace", "build", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "one", "new": "ONE"},
                                {"old": "two", "new": "TWO"},
                            ],
                        }
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "only during repair"):
                run_tool_loop("builder", task, (), workspace, chat)

            self.assertEqual(path.read_text(encoding="utf-8"), "one two")

    def test_repair_builder_cannot_bypass_batch_with_single_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text",
                            "path": "agent.py",
                            "old": "before",
                            "new": "after",
                        }
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "must use replace_text_batch"):
                run_tool_loop(
                    "builder",
                    task,
                    (),
                    workspace,
                    chat,
                    builder_repair_feedback="replace before with after",
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "before")

    def test_repair_overwrite_feedback_requires_batch_action(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "write_file",
                            "path": "agent.py",
                            "content": "after",
                        }
                    ),
                    finish(False),
                )
            )

            run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                builder_repair_feedback="replace before with after",
            )

            feedback = json.loads(chat.calls[1][-1][1])
            self.assertEqual(feedback["required_action"], "replace_text_batch")
            self.assertEqual(path.read_text(encoding="utf-8"), "before")

    def test_successful_repair_requires_an_actual_workspace_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    finish(),
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "before", "new": "after"},
                            ],
                        }
                    ),
                    json.dumps({"action": "run_check"}),
                    finish(),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                check_runner=lambda: (True, "ok"),
                builder_repair_feedback="replace before with after",
            )

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertEqual(
                chat.calls[1][-1],
                (
                    "user",
                    '{"error":"repair_change_required",'
                    '"required_action":"replace_text_batch"}\n',
                ),
            )

    def test_identical_rewrite_does_not_satisfy_repair_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "write_file",
                            "path": "agent.py",
                            "content": "before",
                        }
                    ),
                    finish(),
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "before", "new": "after"},
                            ],
                        }
                    ),
                    json.dumps({"action": "run_check"}),
                    finish(),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                check_runner=lambda: (True, "ok"),
                builder_repair_feedback="replace before with after",
            )

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertEqual(
                chat.calls[1][-1],
                ("user", '{"path":"agent.py","status":"unchanged"}\n'),
            )
            self.assertIn("repair_change_required", chat.calls[2][-1][1])

    def test_builder_cannot_overwrite_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("correct before wrong", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "write_file",
                            "path": "agent.py",
                            "content": "regressed wrong",
                        }
                    ),
                    json.dumps(
                        {
                            "action": "replace_text",
                            "path": "agent.py",
                            "old": "wrong",
                            "new": "fixed",
                        }
                    ),
                    finish(),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
            )

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "correct before fixed")
            feedback = json.loads(chat.calls[1][-1][1])
            self.assertEqual(
                feedback["error"], "existing_file_requires_replace_text"
            )

    def test_repair_builder_must_recheck_after_each_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            check_results = iter(((False, "still bad"), (True, "fixed")))
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "before", "new": "intermediate"},
                            ],
                        }
                    ),
                    json.dumps({"action": "run_check"}),
                    finish(),
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "intermediate", "new": "after"},
                            ],
                        }
                    ),
                    finish(),
                    json.dumps({"action": "run_check"}),
                    finish(),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                check_runner=lambda: next(check_results),
                builder_repair_feedback="replace before with after",
            )

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            self.assertIn("repair_host_check_failed", chat.calls[3][-1][1])
            self.assertIn("host_check_required", chat.calls[5][-1][1])

    def test_invalid_replacement_is_returned_for_bounded_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text",
                            "path": "agent.py",
                            "old": "before",
                            "new": "before",
                        }
                    ),
                    json.dumps(
                        {
                            "action": "replace_text_batch",
                            "path": "agent.py",
                            "replacements": [
                                {"old": "before", "new": "after"},
                            ],
                        }
                    ),
                    json.dumps({"action": "run_check"}),
                    finish(),
                )
            )

            result = run_tool_loop(
                "builder",
                task,
                (),
                workspace,
                chat,
                check_runner=lambda: (True, "fixed"),
                builder_repair_feedback="replace before with after",
            )

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "after")
            invalid_feedback = json.loads(chat.calls[1][-1][1])
            self.assertEqual(invalid_feedback["error"], "invalid_tool_action")
            self.assertIn("old and new must differ", invalid_feedback["detail"])

    def test_parses_only_exact_bounded_action(self):
        content = json.dumps(
            {
                "action": "replace_text",
                "path": "agent.py",
                "old": "before",
                "new": "after",
            }
        )
        self.assertEqual(
            parse_tool_action(content),
            ReplaceTextAction("agent.py", "before", "after"),
        )
        invalid = (
            {"action": "replace_text", "path": "agent.py", "old": "x"},
            {
                "action": "replace_text",
                "path": "agent.py",
                "old": "",
                "new": "x",
            },
            {
                "action": "replace_text",
                "path": "agent.py",
                "old": "x",
                "new": "x",
            },
            {
                "action": "replace_text",
                "path": "agent.py",
                "old": "x",
                "new": "y",
                "count": 1,
            },
        )
        for action in invalid:
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    parse_tool_action(json.dumps(action))

    def test_builder_replaces_one_exact_occurrence_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before middle tail", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text",
                            "path": "agent.py",
                            "old": "middle",
                            "new": "fixed",
                        }
                    ),
                    finish(),
                )
            )

            result = run_tool_loop("builder", task, (), workspace, chat)

            self.assertTrue(result.passed)
            self.assertEqual(path.read_text(encoding="utf-8"), "before fixed tail")
            self.assertEqual(
                chat.calls[1][-1],
                ("user", '{"path":"agent.py","status":"replaced"}\n'),
            )
            self.assertEqual(list(root.glob(".agent.py.*.tmp")), [])

    def test_zero_or_multiple_occurrences_do_not_change_file(self):
        for old, expected_count in (("missing", 0), ("same", 2)):
            with self.subTest(old=old):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / "agent.py"
                    path.write_text("same same", encoding="utf-8")
                    task = SubagentTask("replace", "repair", ("agent.py",))
                    workspace = WorkspaceFileTools(root, task)
                    chat = ScriptedChat(
                        (
                            json.dumps(
                                {
                                    "action": "replace_text",
                                    "path": "agent.py",
                                    "old": old,
                                    "new": "fixed",
                                }
                            ),
                            finish(False),
                        )
                    )

                    run_tool_loop("builder", task, (), workspace, chat)

                    self.assertEqual(path.read_text(encoding="utf-8"), "same same")
                    feedback = json.loads(chat.calls[1][-1][1])
                    self.assertEqual(feedback["occurrences"], expected_count)

    def test_tester_cannot_replace_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("before", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text",
                            "path": "agent.py",
                            "old": "before",
                            "new": "after",
                        }
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "only builder may replace"):
                run_tool_loop(
                    "tester",
                    task,
                    (SubagentResult("builder", True, "built", ()),),
                    workspace,
                    chat,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "before")

    def test_replacement_cannot_exceed_resource_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.py"
            path.write_text("1234", encoding="utf-8")
            task = SubagentTask("replace", "repair", ("agent.py",))
            workspace = WorkspaceFileTools(root, task)
            chat = ScriptedChat(
                (
                    json.dumps(
                        {
                            "action": "replace_text",
                            "path": "agent.py",
                            "old": "4",
                            "new": "456",
                        }
                    ),
                )
            )

            with self.assertRaisesRegex(ValueError, "result exceeds"):
                run_tool_loop(
                    "builder",
                    task,
                    (),
                    workspace,
                    chat,
                    max_file_chars=5,
                )

            self.assertEqual(path.read_text(encoding="utf-8"), "1234")


if __name__ == "__main__":
    unittest.main()
