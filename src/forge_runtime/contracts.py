from dataclasses import dataclass
from typing import Tuple

from .host_policy import HostOnlyDecision, host_decision

@dataclass(frozen=True)
class SubagentTask:
    task_id: str
    objective: str
    allowed_files: Tuple[str, ...]
    read_only_files: Tuple[str, ...] = ()
    
    def __post_init__(self):
        if type(self.task_id) is not str or not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if type(self.objective) is not str or not self.objective.strip():
            raise ValueError("objective must be a non-empty string")
        if type(self.allowed_files) is not tuple:
            raise ValueError("allowed_files must be a tuple")
        for f in self.allowed_files:
            if type(f) is not str or not f.strip():
                raise ValueError("all allowed_files must be non-empty strings")
        if type(self.read_only_files) is not tuple:
            raise ValueError("read_only_files must be a tuple")
        for f in self.read_only_files:
            if type(f) is not str or not f.strip():
                raise ValueError("all read_only_files must be non-empty strings")
        if len(set(self.read_only_files)) != len(self.read_only_files):
            raise ValueError("read_only_files must not contain duplicates")
        if set(self.allowed_files) & set(self.read_only_files):
            raise ValueError("writable and read-only files must not overlap")

@dataclass(frozen=True)
class SubagentResult:
    role: str
    passed: bool
    summary: str
    findings: Tuple[str, ...]
    
    def __post_init__(self):
        if type(self.role) is not str or not self.role.strip():
            raise ValueError("role must be a non-empty string")
        if self.role not in ("builder", "tester", "reviewer"):
            raise ValueError("role must be one of 'builder', 'tester', 'reviewer'")
        if type(self.passed) is not bool:
            raise ValueError("passed must be a boolean")
        if type(self.summary) is not str or not self.summary.strip():
            raise ValueError("summary must be a non-empty string")
        if type(self.findings) is not tuple:
            raise ValueError("findings must be a tuple")
        for f in self.findings:
            if type(f) is not str or not f.strip():
                raise ValueError("all findings must be non-empty strings")

@dataclass(frozen=True)
class PipelineResult:
    results: Tuple[SubagentResult, ...]
    
    def __post_init__(self):
        if type(self.results) is not tuple:
            raise ValueError("results must be a tuple")
        for r in self.results:
            if type(r) is not SubagentResult:
                raise ValueError("all results must be SubagentResult instances")
        
        # Validate semantic pipeline flow immediately upon construction
        if len(self.results) == 0:
            raise ValueError("Pipeline cannot be empty")
        
        roles = [r.role for r in self.results]
        passed = [r.passed for r in self.results]
        
        # Check valid pipeline flows
        if len(self.results) == 1:
            # Only builder with passed=False
            if roles != ["builder"] or passed != [False]:
                raise ValueError("Invalid pipeline flow: only builder with passed=False expected")
        elif len(self.results) == 2:
            # Builder passed=True, tester passed=False
            if roles != ["builder", "tester"] or passed != [True, False]:
                raise ValueError("Invalid pipeline flow: builder passed=True, tester passed=False expected")
        elif len(self.results) == 3:
            # Builder and tester passed=True; reviewer may be True or False
            if roles != ["builder", "tester", "reviewer"]:
                raise ValueError("Invalid pipeline flow: expected builder, tester, reviewer in order")
            if not (passed[0] is True and passed[1] is True):
                raise ValueError("Invalid pipeline flow: builder and tester must both pass")
        else:
            raise ValueError("Invalid pipeline flow: only 1, 2, or 3 results allowed")
    
    @property
    def approved(self) -> bool:
        return host_decision(
            HostOnlyDecision.TASK_SUCCESS,
            len(self.results) == 3 and all(r.passed for r in self.results),
        )
