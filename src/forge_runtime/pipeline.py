from .contracts import SubagentTask, SubagentResult, PipelineResult
from .host_policy import HostOnlyDecision, host_decision


def _role_transition_allowed(result: SubagentResult, expected_role: str) -> bool:
    return host_decision(
        HostOnlyDecision.ROLE_TRANSITION,
        type(result) is SubagentResult and result.role == expected_role,
    )

def run_review_pipeline(task: SubagentTask, executor) -> PipelineResult:
    """
    Run a review pipeline with builder, tester, and reviewer roles.
    
    Args:
        task: The task to execute
        executor: A function that takes (role, task, previous_results) and returns a SubagentResult
        
    Returns:
        PipelineResult containing the results of all executed roles
        
    Raises:
        ValueError: If executor returns invalid results or if role doesn't match expected role
    """
    results = []
    previous_results = ()
    
    # Execute builder
    builder_result = executor("builder", task, previous_results)
    if not _role_transition_allowed(builder_result, "builder"):
        raise ValueError("executor must return the expected SubagentResult role")
    results.append(builder_result)
    if not builder_result.passed:
        return PipelineResult(tuple(results))
    
    # Execute tester
    previous_results = tuple(results)
    tester_result = executor("tester", task, previous_results)
    if not _role_transition_allowed(tester_result, "tester"):
        raise ValueError("executor must return the expected SubagentResult role")
    results.append(tester_result)
    if not tester_result.passed:
        return PipelineResult(tuple(results))
    
    # Execute reviewer
    previous_results = tuple(results)
    reviewer_result = executor("reviewer", task, previous_results)
    if not _role_transition_allowed(reviewer_result, "reviewer"):
        raise ValueError("executor must return the expected SubagentResult role")
    results.append(reviewer_result)
    
    return PipelineResult(tuple(results))
