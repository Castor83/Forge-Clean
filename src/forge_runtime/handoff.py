import json
from .contracts import SubagentTask, SubagentResult

def build_role_handoff(role: str, task: SubagentTask, previous_results: tuple[SubagentResult, ...]) -> str:
    """
    Build a compact deterministic handoff for subagent roles.

    Args:
        role: The role to build handoff for ('builder', 'tester', or 'reviewer')
        task: The current task
        previous_results: Previous results in order

    Returns:
        JSON string with compact handoff data

    Raises:
        ValueError: If arguments don't meet requirements
    """
    # Type checks
    if type(role) is not str:
        raise ValueError("role must be a string")
    if type(task) is not SubagentTask:
        raise ValueError("task must be a SubagentTask")
    if type(previous_results) is not tuple:
        raise ValueError("previous_results must be a tuple")
    for r in previous_results:
        if type(r) is not SubagentResult:
            raise ValueError("all previous_results must be SubagentResult instances")

    # Validate role
    if role not in ("builder", "tester", "reviewer"):
        raise ValueError("role must be one of 'builder', 'tester', 'reviewer'")

    # Validate previous results based on role
    if role == "builder":
        if len(previous_results) != 0:
            raise ValueError("builder must not have previous results")
    elif role == "tester":
        if len(previous_results) != 1:
            raise ValueError("tester must have exactly one previous result")
        if previous_results[0].role != "builder" or previous_results[0].passed is not True:
            raise ValueError("tester must follow a passed builder")
    elif role == "reviewer":
        if len(previous_results) != 2:
            raise ValueError("reviewer must have exactly two previous results")
        if previous_results[0].role != "builder" or previous_results[0].passed is not True:
            raise ValueError("reviewer must follow a passed builder")
        if previous_results[1].role != "tester" or previous_results[1].passed is not True:
            raise ValueError("reviewer must follow a passed tester")

    # Build the handoff data
    handoff_data = {
        "completed_roles": [result.role for result in previous_results],
        "role": role,
        "task_id": task.task_id,
        "objective": task.objective,
        "allowed_files": list(task.allowed_files),
    }
    if task.read_only_files:
        handoff_data["read_only_files"] = list(task.read_only_files)

    # Return compact JSON with exact formatting
    return json.dumps(handoff_data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
