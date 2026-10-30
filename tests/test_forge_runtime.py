import unittest
from src.forge_runtime import SubagentTask, SubagentResult, PipelineResult, run_review_pipeline

class TestForgeRuntime(unittest.TestCase):
    
    def test_subagent_task_valid(self):
        task = SubagentTask("task1", "objective1", ("file1.txt", "file2.txt"))
        self.assertEqual(task.task_id, "task1")
        self.assertEqual(task.objective, "objective1")
        self.assertEqual(task.allowed_files, ("file1.txt", "file2.txt"))
    
    def test_subagent_task_invalid_fields(self):
        with self.assertRaises(ValueError):
            SubagentTask("", "objective1", ("file1.txt",))
        with self.assertRaises(ValueError):
            SubagentTask("task1", "", ("file1.txt",))
        with self.assertRaises(ValueError):
            SubagentTask("task1", "objective1", None)
    
    def test_subagent_result_valid(self):
        result = SubagentResult("builder", True, "summary1", ("find1", "find2"))
        self.assertEqual(result.role, "builder")
        self.assertTrue(result.passed)
        self.assertEqual(result.summary, "summary1")
        self.assertEqual(result.findings, ("find1", "find2"))
    
    def test_subagent_result_invalid_fields(self):
        with self.assertRaises(ValueError):
            SubagentResult("", True, "summary1", ())
        with self.assertRaises(ValueError):
            SubagentResult("invalid_role", True, "summary1", ())
        with self.assertRaises(ValueError):
            SubagentResult("builder", "not_a_bool", "summary1", ())
        with self.assertRaises(ValueError):
            SubagentResult("builder", True, "", ())
    
    def test_subagent_result_exact_types(self):
        # Test that exact types are enforced (not isinstance)
        with self.assertRaises(ValueError):
            SubagentResult(123, True, "summary1", ())  # role should be str
        with self.assertRaises(ValueError):
            SubagentResult("builder", 123, "summary1", ())  # passed should be bool
        with self.assertRaises(ValueError):
            SubagentResult("builder", True, 123, ())  # summary should be str
        with self.assertRaises(ValueError):
            SubagentResult("builder", True, "summary1", (123,))  # findings should contain only strings
    
    def test_subagent_result_exact_types_inheritance(self):
        # Test that exact types are enforced for tuple fields
        with self.assertRaises(ValueError):
            SubagentResult("builder", True, "summary1", ("find1", 123))  # findings should contain only strings
        
    def test_subagent_result_exact_types_subclass(self):
        # Test that exact types are enforced - str subclasses
        class MyStr(str):
            pass
        
        # These should fail because we want exact str type, not subclass
        with self.assertRaises(ValueError):
            SubagentResult(MyStr("builder"), True, "summary1", ())  # role should be exact str
        with self.assertRaises(ValueError):
            SubagentResult("builder", True, MyStr("summary1"), ())  # summary should be exact str
        
        # These should also fail because we want exact tuple type, not subclass
        with self.assertRaises(ValueError):
            class MyTuple(tuple):
                pass
            result = SubagentResult("builder", True, "summary1", MyTuple(("find1",)))  # findings should be exact tuple
    
    def test_pipeline_result_approved(self):
        # Test that exact types are enforced for results tuple
        with self.assertRaises(ValueError):
            PipelineResult(("not_a_result",))
        
        builder = SubagentResult("builder", True, "build", ())
        tester = SubagentResult("tester", True, "test", ())
        reviewer = SubagentResult("reviewer", True, "review", ())
        pipeline = PipelineResult((builder, tester, reviewer))
        self.assertTrue(pipeline.approved)
    
    def test_pipeline_result_approved_with_subclass(self):
        # Test that exact types are enforced for results tuple - subclasses
        class MySubagentResult(SubagentResult):
            pass
        
        builder = SubagentResult("builder", True, "build", ())
        tester = SubagentResult("tester", True, "test", ())
        reviewer = SubagentResult("reviewer", True, "review", ())
        
        # This should fail because we want exact SubagentResult type
        with self.assertRaises(ValueError):
            PipelineResult((builder, tester, MySubagentResult("reviewer", True, "review", ())))
    
    def test_pipeline_result_not_approved(self):
        builder = SubagentResult("builder", True, "build", ())
        tester = SubagentResult("tester", False, "test", ())
        reviewer = SubagentResult("reviewer", True, "review", ())
        # This should now raise ValueError because tester failed but reviewer is present
        # According to new rules: builder=True/tester=False/reviewer - invalid pipeline flow
        with self.assertRaises(ValueError):
            PipelineResult((builder, tester, reviewer))
    
    def test_pipeline_result_invalid_flow_reviewer_passed_false(self):
        # Test the case where we have builder and tester passed=True but reviewer passed=False
        # This is actually valid according to new rules - reviewer can be False
        builder = SubagentResult("builder", True, "build", ())
        tester = SubagentResult("tester", True, "test", ())
        reviewer = SubagentResult("reviewer", False, "review", ())
        pipeline = PipelineResult((builder, tester, reviewer))
        self.assertFalse(pipeline.approved)
        
    def test_pipeline_result_invalid_flow_wrong_order(self):
        # Test wrong order - this should raise ValueError
        builder = SubagentResult("builder", True, "build", ())
        reviewer = SubagentResult("reviewer", True, "review", ())
        tester = SubagentResult("tester", True, "test", ())
        with self.assertRaises(ValueError):
            PipelineResult((builder, reviewer, tester))
    
    def test_pipeline_result_invalid_flow_missing_role(self):
        # Test missing role - this should raise ValueError
        builder = SubagentResult("builder", True, "build", ())
        tester = SubagentResult("tester", True, "test", ())
        # Missing reviewer - this should be invalid
        with self.assertRaises(ValueError):
            PipelineResult((builder, tester))
    
    def test_pipeline_result_invalid_flow_early_error_later_result(self):
        # Test case where we have an early error but later results - this should raise ValueError
        builder = SubagentResult("builder", False, "build failed", ())
        tester = SubagentResult("tester", True, "test passed", ())
        # This is invalid because tester cannot pass when builder failed
        with self.assertRaises(ValueError):
            PipelineResult((builder, tester))
    
    def test_pipeline_result_wrong_order(self):
        builder = SubagentResult("builder", True, "build", ())
        reviewer = SubagentResult("reviewer", True, "review", ())
        tester = SubagentResult("tester", True, "test", ())
        # This should raise ValueError because of wrong order
        with self.assertRaises(ValueError):
            PipelineResult((builder, reviewer, tester))
    
    def test_pipeline_result_wrong_number_of_results(self):
        builder = SubagentResult("builder", True, "build", ())
        tester = SubagentResult("tester", True, "test", ())
        # This should raise ValueError because of wrong number of results
        with self.assertRaises(ValueError):
            PipelineResult((builder, tester))
    
    def test_run_review_pipeline_exact_order(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                # Verify previous_results is empty tuple for builder
                self.assertEqual(previous_results, ())
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                # Verify previous_results contains only builder result
                self.assertEqual(len(previous_results), 1)
                self.assertEqual(previous_results[0].role, "builder")
                self.assertTrue(previous_results[0].passed)
                return SubagentResult("tester", True, "tested", ())
            elif role == "reviewer":
                # Verify previous_results contains builder and tester results
                self.assertEqual(len(previous_results), 2)
                self.assertEqual(previous_results[0].role, "builder")
                self.assertTrue(previous_results[0].passed)
                self.assertEqual(previous_results[1].role, "tester")
                self.assertTrue(previous_results[1].passed)
                return SubagentResult("reviewer", True, "reviewed", ())
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertTrue(result.approved)
        self.assertEqual(len(result.results), 3)
        self.assertEqual(result.results[0].role, "builder")
        self.assertEqual(result.results[1].role, "tester")
        self.assertEqual(result.results[2].role, "reviewer")
        # Verify exact call order
        self.assertEqual(call_log, ["builder", "tester", "reviewer"])
    
    def test_run_review_pipeline_stop_at_builder_failure(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                # Verify previous_results is empty tuple for builder
                self.assertEqual(previous_results, ())
                return SubagentResult("builder", False, "failed build", ())
            elif role == "tester":
                # This should not be called
                raise ValueError("Tester should not be called")
            elif role == "reviewer":
                # This should not be called
                raise ValueError("Reviewer should not be called")
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertFalse(result.approved)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].role, "builder")
        self.assertFalse(result.results[0].passed)
        # Verify that only builder was called
        self.assertEqual(call_log, ["builder"])
    
    def test_run_review_pipeline_stop_at_tester_failure(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                # Verify previous_results is empty tuple for builder
                self.assertEqual(previous_results, ())
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                # Verify previous_results contains only builder result
                self.assertEqual(len(previous_results), 1)
                self.assertEqual(previous_results[0].role, "builder")
                self.assertTrue(previous_results[0].passed)
                return SubagentResult("tester", False, "failed test", ())
            elif role == "reviewer":
                # This should not be called
                raise ValueError("Reviewer should not be called")
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertFalse(result.approved)
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.results[0].role, "builder")
        self.assertTrue(result.results[0].passed)
        self.assertEqual(result.results[1].role, "tester")
        self.assertFalse(result.results[1].passed)
        # Verify that builder and tester were called
        self.assertEqual(call_log, ["builder", "tester"])
    
    def test_run_review_pipeline_stop_at_reviewer_failure(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                return SubagentResult("tester", True, "tested", ())
            elif role == "reviewer":
                return SubagentResult("reviewer", False, "failed review", ())
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertFalse(result.approved)
        self.assertEqual(len(result.results), 3)
        self.assertEqual(result.results[0].role, "builder")
        self.assertTrue(result.results[0].passed)
        self.assertEqual(result.results[1].role, "tester")
        self.assertTrue(result.results[1].passed)
        self.assertEqual(result.results[2].role, "reviewer")
        self.assertFalse(result.results[2].passed)
        # Verify that all three roles were called
        self.assertEqual(call_log, ["builder", "tester", "reviewer"])
    
    def test_run_review_pipeline_exact_order_and_content(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                # Verify previous_results is empty tuple for builder
                self.assertEqual(previous_results, ())
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                # Verify previous_results contains only builder result
                self.assertEqual(len(previous_results), 1)
                self.assertEqual(previous_results[0].role, "builder")
                self.assertTrue(previous_results[0].passed)
                return SubagentResult("tester", True, "tested", ())
            elif role == "reviewer":
                # Verify previous_results contains builder and tester results
                self.assertEqual(len(previous_results), 2)
                self.assertEqual(previous_results[0].role, "builder")
                self.assertTrue(previous_results[0].passed)
                self.assertEqual(previous_results[1].role, "tester")
                self.assertTrue(previous_results[1].passed)
                return SubagentResult("reviewer", True, "reviewed", ())
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertTrue(result.approved)
        self.assertEqual(len(result.results), 3)
        self.assertEqual(result.results[0].role, "builder")
        self.assertEqual(result.results[1].role, "tester")
        self.assertEqual(result.results[2].role, "reviewer")
        # Verify exact call order
        self.assertEqual(call_log, ["builder", "tester", "reviewer"])
    
    def test_run_review_pipeline_stop_at_builder_failure_exact_call_order(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                # Verify previous_results is empty tuple for builder
                self.assertEqual(previous_results, ())
                return SubagentResult("builder", False, "failed build", ())
            elif role == "tester":
                # This should not be called - verify no call was made
                self.fail("Tester should not be called when builder fails")
            elif role == "reviewer":
                # This should not be called - verify no call was made
                self.fail("Reviewer should not be called when builder fails")
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertFalse(result.approved)
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].role, "builder")
        self.assertFalse(result.results[0].passed)
        # Verify that only builder was called
        self.assertEqual(call_log, ["builder"])
    
    def test_run_review_pipeline_stop_at_tester_failure_exact_call_order(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        call_log = []  # Track which roles are called
        
        def executor(role, task, previous_results):
            call_log.append(role)
            if role == "builder":
                # Verify previous_results is empty tuple for builder
                self.assertEqual(previous_results, ())
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                # Verify previous_results contains only builder result
                self.assertEqual(len(previous_results), 1)
                self.assertEqual(previous_results[0].role, "builder")
                self.assertTrue(previous_results[0].passed)
                return SubagentResult("tester", False, "failed test", ())
            elif role == "reviewer":
                # This should not be called - verify no call was made
                self.fail("Reviewer should not be called when tester fails")
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertFalse(result.approved)
        self.assertEqual(len(result.results), 2)
        self.assertEqual(result.results[0].role, "builder")
        self.assertTrue(result.results[0].passed)
        self.assertEqual(result.results[1].role, "tester")
        self.assertFalse(result.results[1].passed)
        # Verify that builder and tester were called
        self.assertEqual(call_log, ["builder", "tester"])
    
    def test_run_review_pipeline_invalid_result_type(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        def executor(role, task, previous_results):
            return "not_a_result"  # Invalid return type
        
        with self.assertRaises(ValueError):
            run_review_pipeline(task, executor)
    
    def test_run_review_pipeline_wrong_role(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        def executor(role, task, previous_results):
            if role == "builder":
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                # Return wrong role
                return SubagentResult("builder", True, "wrong role", ())  # Should be tester
            else:
                raise ValueError("Unexpected role")
        
        with self.assertRaises(ValueError):
            run_review_pipeline(task, executor)
    
    def test_run_review_pipeline_exact_types(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        def executor(role, task, previous_results):
            # Test that exact types are enforced for return values
            if role == "builder":
                return "not_a_result"  # Invalid return type
            else:
                raise ValueError("Unexpected role")
        
        with self.assertRaises(ValueError):
            run_review_pipeline(task, executor)
    
    def test_immutable_dataclasses(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        with self.assertRaises(AttributeError):
            task.task_id = "new_id"
        
        result = SubagentResult("builder", True, "summary", ("find1",))
        with self.assertRaises(AttributeError):
            result.role = "tester"
    
    def test_immutable_previous_results(self):
        task = SubagentTask("task1", "objective1", ("file1.txt",))
        
        def executor(role, task, previous_results):
            # Verify that previous_results is immutable (tuple)
            self.assertIsInstance(previous_results, tuple)
            if role == "builder":
                return SubagentResult("builder", True, "built", ())
            elif role == "tester":
                return SubagentResult("tester", True, "tested", ())
            elif role == "reviewer":
                return SubagentResult("reviewer", True, "reviewed", ())
            else:
                raise ValueError("Unexpected role")
        
        result = run_review_pipeline(task, executor)
        self.assertTrue(result.approved)

if __name__ == '__main__':
    unittest.main()