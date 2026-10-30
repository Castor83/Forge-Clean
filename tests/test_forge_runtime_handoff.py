import unittest
from src.forge_runtime.contracts import SubagentTask, SubagentResult
from src.forge_runtime.handoff import build_role_handoff

class TestBuildRoleHandoff(unittest.TestCase):

    def test_builder_handoff(self):
        """Test builder handoff with no previous results"""
        task = SubagentTask(
            task_id="task1",
            objective="Build a new feature",
            allowed_files=("src/main.py", "src/utils.py")
        )

        result = build_role_handoff("builder", task, ())

        expected = (
            '{"allowed_files":["src/main.py","src/utils.py"],'
            '"completed_roles":[],"objective":"Build a new feature",'
            '"role":"builder","task_id":"task1"}\n'
        )

        self.assertEqual(result, expected)

    def test_tester_handoff(self):
        """Test tester handoff with one previous builder result"""
        task = SubagentTask(
            task_id="task2",
            objective="Test the new feature",
            allowed_files=("src/main.py", "tests/test_main.py")
        )

        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Feature built successfully",
            findings=("Feature implemented", "Code reviewed")
        )

        result = build_role_handoff("tester", task, (builder_result,))

        expected = (
            '{"allowed_files":["src/main.py","tests/test_main.py"],'
            '"completed_roles":["builder"],'
            '"objective":"Test the new feature",'
            '"role":"tester","task_id":"task2"}\n'
        )

        self.assertEqual(result, expected)

    def test_reviewer_handoff(self):
        """Test reviewer handoff with two previous results"""
        task = SubagentTask(
            task_id="task3",
            objective="Review the implementation",
            allowed_files=("src/main.py", "tests/test_main.py", "docs/README.md")
        )

        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Feature built successfully",
            findings=("Feature implemented", "Code reviewed")
        )

        tester_result = SubagentResult(
            role="tester",
            passed=True,
            summary="All tests passed",
            findings=("Unit tests passed", "Integration tests passed")
        )

        result = build_role_handoff("reviewer", task, (builder_result, tester_result))

        expected = (
            '{"allowed_files":["src/main.py","tests/test_main.py",'
            '"docs/README.md"],"completed_roles":["builder","tester"],'
            '"objective":"Review the implementation",'
            '"role":"reviewer","task_id":"task3"}\n'
        )

        self.assertEqual(result, expected)

    def test_deterministic_output(self):
        """Test that output is deterministic"""
        task = SubagentTask(
            task_id="task4",
            objective="Another feature",
            allowed_files=("src/feature.py",)
        )

        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Built successfully",
            findings=("Implemented", "Tested")
        )

        result1 = build_role_handoff("tester", task, (builder_result,))
        result2 = build_role_handoff("tester", task, (builder_result,))

        self.assertEqual(result1, result2)

    def test_input_immutability(self):
        """Test that inputs are not modified"""
        task = SubagentTask(
            task_id="task5",
            objective="Test immutability",
            allowed_files=("src/test.py",)
        )

        original_task = SubagentTask(
            task_id="task5",
            objective="Test immutability",
            allowed_files=("src/test.py",)
        )

        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Built",
            findings=("Done",)
        )

        original_task_id = task.task_id
        original_objective = task.objective
        original_allowed_files = task.allowed_files

        build_role_handoff("tester", task, (builder_result,))

        # Check that inputs are unchanged
        self.assertEqual(task.task_id, original_task_id)
        self.assertEqual(task.objective, original_objective)
        self.assertEqual(task.allowed_files, original_allowed_files)
        self.assertEqual(task, original_task)

    def test_invalid_role(self):
        """Test invalid role raises ValueError"""
        task = SubagentTask(
            task_id="task6",
            objective="Test",
            allowed_files=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("invalid", task, ())

        self.assertIn("role must be one of 'builder', 'tester', 'reviewer'", str(context.exception))

    def test_invalid_builder_previous_results(self):
        """Test builder with invalid previous results"""
        task = SubagentTask(
            task_id="task7",
            objective="Test",
            allowed_files=()
        )

        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Built",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("builder", task, (builder_result,))

        self.assertIn("builder must not have previous results", str(context.exception))

    def test_invalid_tester_previous_results(self):
        """Test tester with invalid previous results"""
        task = SubagentTask(
            task_id="task8",
            objective="Test",
            allowed_files=()
        )

        # Test case 1: No previous results
        with self.assertRaises(ValueError) as context:
            build_role_handoff("tester", task, ())

        self.assertIn("tester must have exactly one previous result", str(context.exception))

        # Test case 2: Wrong role
        reviewer_result = SubagentResult(
            role="reviewer",
            passed=True,
            summary="Reviewed",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("tester", task, (reviewer_result,))

        self.assertIn("tester must follow a passed builder", str(context.exception))

        # Test case 3: Builder failed
        builder_failed = SubagentResult(
            role="builder",
            passed=False,
            summary="Failed",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("tester", task, (builder_failed,))

        self.assertIn("tester must follow a passed builder", str(context.exception))

    def test_invalid_reviewer_previous_results(self):
        """Test reviewer with invalid previous results"""
        task = SubagentTask(
            task_id="task9",
            objective="Test",
            allowed_files=()
        )

        # Test case 1: Wrong number of previous results
        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Built",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("reviewer", task, (builder_result,))

        self.assertIn("reviewer must have exactly two previous results", str(context.exception))

        # Test case 2: Wrong first role
        tester_result = SubagentResult(
            role="tester",
            passed=True,
            summary="Tested",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("reviewer", task, (tester_result,))

        # This should fail because there's only one result, not two
        self.assertIn("reviewer must have exactly two previous results", str(context.exception))

        # Test case 3: Builder failed
        builder_failed = SubagentResult(
            role="builder",
            passed=False,
            summary="Failed",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("reviewer", task, (builder_failed, tester_result))

        self.assertIn("reviewer must follow a passed builder", str(context.exception))

        # Test case 4: Wrong second role
        with self.assertRaises(ValueError) as context:
            build_role_handoff("reviewer", task, (builder_result, builder_result))

        self.assertIn("reviewer must follow a passed tester", str(context.exception))

        # Test case 5: Tester failed
        tester_failed = SubagentResult(
            role="tester",
            passed=False,
            summary="Failed",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("reviewer", task, (builder_result, tester_failed))

        self.assertIn("reviewer must follow a passed tester", str(context.exception))

    def test_invalid_types(self):
        """Test that invalid types raise ValueError"""
        task = SubagentTask(
            task_id="task10",
            objective="Test",
            allowed_files=()
        )

        # Test role not string
        with self.assertRaises(ValueError) as context:
            build_role_handoff(123, task, ())

        self.assertIn("role must be a string", str(context.exception))

        # Test task not SubagentTask
        with self.assertRaises(ValueError) as context:
            build_role_handoff("builder", "not_a_task", ())

        self.assertIn("task must be a SubagentTask", str(context.exception))

        # Test previous_results not tuple
        with self.assertRaises(ValueError) as context:
            build_role_handoff("builder", task, [])

        self.assertIn("previous_results must be a tuple", str(context.exception))

        # Test invalid result type in previous_results
        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Built",
            findings=()
        )

        with self.assertRaises(ValueError) as context:
            build_role_handoff("tester", task, (builder_result, "not_a_result"))

        self.assertIn("all previous_results must be SubagentResult instances", str(context.exception))

    def test_subclass_types_are_rejected(self):
        """Test that subclasses of all exact input types are rejected"""
        class FakeStr(str):
            pass

        class FakeTuple(tuple):
            pass

        class FakeTask(SubagentTask):
            pass

        class FakeResult(SubagentResult):
            pass

        task = SubagentTask(
            task_id="task11",
            objective="Test",
            allowed_files=()
        )

        builder_result = SubagentResult(
            role="builder",
            passed=True,
            summary="Built",
            findings=()
        )

        with self.assertRaises(ValueError):
            build_role_handoff(FakeStr("builder"), task, ())

        with self.assertRaises(ValueError):
            build_role_handoff("builder", task, FakeTuple())

        fake_task = FakeTask(
            task_id="task11",
            objective="Test",
            allowed_files=()
        )
        with self.assertRaises(ValueError):
            build_role_handoff("builder", fake_task, ())

        fake_result = FakeResult(
            role="builder",
            passed=True,
            summary="Built",
            findings=()
        )
        with self.assertRaises(ValueError):
            build_role_handoff("tester", task, (fake_result,))

        # This should work as expected
        self.assertEqual(
            build_role_handoff("tester", task, (builder_result,)),
            '{"allowed_files":[],"completed_roles":["builder"],'
            '"objective":"Test","role":"tester","task_id":"task11"}\n'
        )

if __name__ == '__main__':
    unittest.main()
