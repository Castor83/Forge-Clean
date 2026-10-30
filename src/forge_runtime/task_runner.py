import ast
import json
import math
from dataclasses import dataclass
from pathlib import Path

from .contracts import PipelineResult, SubagentTask
from .host_suites import required_files_for_suite, run_host_suite
from .pipeline import run_review_pipeline
from .tool_executor import ForgeToolExecutor
from .tool_loop import HostCheckFailure
from .workspace_tools import WorkspaceFileTools


_CHECK_KINDS = (
    "file_exists",
    "python_syntax",
    "contains_text",
    "python_function_cases",
)
RESTRICTED_FUNCTION_BUILTINS = frozenset({"bool", "isinstance", "str"})
RESTRICTED_EXPRESSION_NODES = 10000
RESTRICTED_VALUE_BUDGET = 65536
RESTRICTED_INTEGER_BITS = 4096
# These nodes only select/combine already counted values or describe syntax.
# New execution syntax needs an explicit resource rule as well.
RESTRICTED_RESOURCE_STRUCTURAL_NODES = frozenset({
    ast.Load, ast.BoolOp, ast.BinOp, ast.JoinedStr, ast.FormattedValue,
    ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Is, ast.IsNot,
    ast.In, ast.NotIn, ast.Add,
})
RESTRICTED_RESOURCE_FIXED_NODES = frozenset({
    ast.Attribute, ast.Call, ast.Compare, ast.UnaryOp,
})


@dataclass(frozen=True)
class FunctionCase:
    args: tuple[object, ...]
    expected: object

    def __post_init__(self) -> None:
        if type(self.args) is not tuple:
            raise ValueError("function case args must be a tuple")
        if not all(_is_safe_scalar(value) for value in self.args):
            raise ValueError("function case args must contain only JSON scalars")
        if not _is_safe_scalar(self.expected):
            raise ValueError("function case expected must be a JSON scalar")


@dataclass(frozen=True)
class StaticCheck:
    kind: str
    path: str
    text: str | None = None
    function: str | None = None
    cases: tuple[FunctionCase, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _CHECK_KINDS:
            raise ValueError("check kind is not supported")
        if type(self.path) is not str or not self.path.strip():
            raise ValueError("check path must be a non-empty string")
        if self.kind == "contains_text":
            if type(self.text) is not str or not self.text:
                raise ValueError("contains_text check requires non-empty text")
            if self.function is not None or self.cases is not None:
                raise ValueError("contains_text check has unsupported fields")
        elif self.kind == "python_function_cases":
            if (
                type(self.function) is not str
                or not self.function.isidentifier()
                or self.function.startswith("_")
            ):
                raise ValueError("python_function_cases requires a public function")
            if type(self.cases) is not tuple or not self.cases:
                raise ValueError("python_function_cases requires non-empty cases")
            if any(type(case) is not FunctionCase for case in self.cases):
                raise ValueError("all function cases must be FunctionCase instances")
            if self.text is not None:
                raise ValueError("python_function_cases may not define text")
        elif self.text is not None or self.function is not None or self.cases is not None:
            raise ValueError("file check has unsupported fields")


@dataclass(frozen=True)
class HostSuiteCheck:
    kind: str
    suite: str

    def __post_init__(self) -> None:
        if self.kind != "host_suite":
            raise ValueError("host suite check kind must be host_suite")
        required_files_for_suite(self.suite)


@dataclass(frozen=True)
class ForgeTaskSpec:
    task: SubagentTask
    checks: tuple[StaticCheck | HostSuiteCheck, ...]

    def __post_init__(self) -> None:
        if type(self.task) is not SubagentTask:
            raise ValueError("task must be a SubagentTask")
        if not self.task.allowed_files:
            raise ValueError("task must allow at least one file")
        if len(set(self.task.allowed_files)) != len(self.task.allowed_files):
            raise ValueError("allowed_files must not contain duplicates")
        if type(self.checks) is not tuple or not self.checks:
            raise ValueError("checks must be a non-empty tuple")
        checked_paths: set[str] = set()
        readable_paths = set(self.task.allowed_files) | set(
            self.task.read_only_files
        )
        for check in self.checks:
            if type(check) is StaticCheck:
                if check.path not in self.task.allowed_files:
                    raise ValueError("every check path must be an allowed file")
                checked_paths.add(check.path)
            elif type(check) is HostSuiteCheck:
                suite_paths = set(required_files_for_suite(check.suite))
                if not suite_paths.issubset(readable_paths):
                    raise ValueError("host suite files must all be task-readable")
                checked_paths.update(suite_paths & set(self.task.allowed_files))
            else:
                raise ValueError("all checks must be registered check instances")
        if checked_paths != set(self.task.allowed_files):
            raise ValueError("every allowed file must have at least one check")


def parse_task_spec(content: str) -> ForgeTaskSpec:
    if type(content) is not str:
        raise ValueError("task specification must be a string")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("task specification must be valid JSON") from exc
    required_fields = {
        "task_id",
        "objective",
        "allowed_files",
        "checks",
    }
    if type(data) is not dict or set(data) not in (
        required_fields,
        required_fields | {"read_only_files"},
    ):
        raise ValueError("task specification has missing or extra fields")
    if type(data["allowed_files"]) is not list:
        raise ValueError("allowed_files must be a list")
    if type(data["checks"]) is not list:
        raise ValueError("checks must be a list")
    if "read_only_files" in data and type(data["read_only_files"]) is not list:
        raise ValueError("read_only_files must be a list")

    checks = tuple(_parse_check(item) for item in data["checks"])
    task = SubagentTask(
        task_id=data["task_id"],
        objective=data["objective"],
        allowed_files=tuple(data["allowed_files"]),
        read_only_files=tuple(data.get("read_only_files", ())),
    )
    return ForgeTaskSpec(task=task, checks=checks)


def run_forge_task(
    root: Path,
    spec: ForgeTaskSpec,
    chat,
    max_steps: int = 12,
    max_file_chars: int = 20_000,
    max_call_seconds: float = 300.0,
    max_elapsed_seconds: float = 900.0,
    max_repair_attempts: int = 0,
    on_progress=None,
    execution_copy_stage: str | None = None,
) -> PipelineResult:
    if not isinstance(root, Path):
        raise ValueError("root must be a pathlib.Path")
    if type(spec) is not ForgeTaskSpec:
        raise ValueError("spec must be a ForgeTaskSpec")
    if (
        type(max_repair_attempts) is not int
        or not 0 <= max_repair_attempts <= 2
    ):
        raise ValueError("max_repair_attempts must be an integer from 0 to 2")
    workspace = WorkspaceFileTools(root, spec.task)
    if execution_copy_stage is not None:
        binding = {
            "validator": ("forge-execution-contract", "execution_contract_v1"),
            "active": ("forge-active-execution", "active_execution_v1"),
        }.get(execution_copy_stage)
        if (binding is None or spec.task.task_id != binding[0] or not any(
                type(check) is HostSuiteCheck and check.suite == binding[1] for check in spec.checks)):
            raise ValueError("execution copy protocol requires its bound task and host suite")
    check_runner = _StaticCheckRunner(workspace, spec.checks)
    executor = ForgeToolExecutor(
        root=root,
        chat=chat,
        max_steps=max_steps,
        max_file_chars=max_file_chars,
        max_call_seconds=max_call_seconds,
        max_elapsed_seconds=max_elapsed_seconds,
        check_runner=check_runner,
        execution_copy_stage=execution_copy_stage,
    )
    remaining_repair_attempts = max_repair_attempts
    if on_progress is not None and not callable(on_progress):
        raise ValueError("on_progress must be callable or None")

    def execute_role(role, task, previous_results):
        if on_progress is not None:
            on_progress(role, "started")
        result = executor(role, task, previous_results)
        if on_progress is not None:
            on_progress(role, "passed" if result.passed else "failed")
        return result
    if _all_allowed_files_exist(workspace, spec.task.allowed_files):
        preflight_passed, preflight_detail = check_runner()
        if not preflight_passed and remaining_repair_attempts > 0:
            executor.set_builder_repair_feedback(preflight_detail)
            remaining_repair_attempts -= 1
    for attempt in range(remaining_repair_attempts + 1):
        check_runner.reset_last_result()
        rejected_claim: HostCheckFailure | None = None
        try:
            result = run_review_pipeline(spec.task, execute_role)
        except HostCheckFailure as exc:
            rejected_claim = exc
            failure_detail = exc.detail
        else:
            if result.approved:
                return result
            last_check = check_runner.last_result
            if last_check is None:
                last_check = check_runner()
            if last_check[0] is not False:
                return result
            failure_detail = last_check[1]

        if attempt == remaining_repair_attempts:
            if rejected_claim is not None:
                raise rejected_claim
            return result
        executor.set_builder_repair_feedback(failure_detail)

    raise RuntimeError("unreachable repair loop state")


def verify_forge_task(root: Path, spec: ForgeTaskSpec) -> tuple[bool, str]:
    """Independently rerun fixed host checks against the actual saved files."""
    return _StaticCheckRunner(WorkspaceFileTools(root, spec.task), spec.checks)()


def _all_allowed_files_exist(
    workspace: WorkspaceFileTools,
    allowed_files: tuple[str, ...],
) -> bool:
    for path in allowed_files:
        try:
            workspace.read_text(path)
        except FileNotFoundError:
            return False
    return True


def format_run_report(result: PipelineResult) -> str:
    if type(result) is not PipelineResult:
        raise ValueError("result must be a PipelineResult")
    report = {
        "approved": result.approved,
        "results": [
            {
                "findings": list(item.findings),
                "passed": item.passed,
                "role": item.role,
                "summary": item.summary,
            }
            for item in result.results
        ],
    }
    return json.dumps(
        report,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _parse_check(value: object) -> StaticCheck | HostSuiteCheck:
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise ValueError("each check must be an object with a kind")
    if value["kind"] == "contains_text":
        if set(value) != {"kind", "path", "text"}:
            raise ValueError("contains_text check has missing or extra fields")
        return StaticCheck(value["kind"], value["path"], value["text"])
    if value["kind"] == "python_function_cases":
        if set(value) != {"kind", "path", "function", "cases"}:
            raise ValueError(
                "python_function_cases check has missing or extra fields"
            )
        if type(value["cases"]) is not list:
            raise ValueError("python_function_cases cases must be a list")
        cases = tuple(_parse_function_case(case) for case in value["cases"])
        return StaticCheck(
            kind=value["kind"],
            path=value["path"],
            function=value["function"],
            cases=cases,
        )
    if value["kind"] == "host_suite":
        if set(value) != {"kind", "suite"}:
            raise ValueError("host_suite check has missing or extra fields")
        return HostSuiteCheck(kind=value["kind"], suite=value["suite"])
    if set(value) != {"kind", "path"}:
        raise ValueError("file check has missing or extra fields")
    return StaticCheck(value["kind"], value["path"])


class _StaticCheckRunner:
    def __init__(
        self,
        workspace: WorkspaceFileTools,
        checks: tuple[StaticCheck | HostSuiteCheck, ...],
    ) -> None:
        self._workspace = workspace
        self._checks = checks
        self.last_result: tuple[bool, str] | None = None

    def reset_last_result(self) -> None:
        self.last_result = None

    def __call__(self) -> tuple[bool, str]:
        result = self._run_checks()
        self.last_result = result
        return result

    def _run_checks(self) -> tuple[bool, str]:
        for index, check in enumerate(self._checks, start=1):
            if type(check) is HostSuiteCheck:
                passed, detail = run_host_suite(check.suite, self._workspace)
                if not passed:
                    return False, (
                        f"check {index}/host_suite/{check.suite} failed: {detail}"
                    )
                continue
            try:
                content = self._workspace.read_text(check.path)
            except FileNotFoundError:
                return False, self._failure(index, check, "file is missing")

            if check.kind == "python_syntax":
                try:
                    ast.parse(content, filename=check.path)
                except SyntaxError as exc:
                    detail = f"invalid Python syntax at line {exc.lineno or 0}"
                    return False, self._failure(index, check, detail)
                except RecursionError:
                    return False, self._failure(index, check, "Python syntax exceeds parser complexity limit")
            elif check.kind == "python_function_cases":
                failure = _run_restricted_function_cases(content, check)
                if failure is not None:
                    return False, self._failure(index, check, failure)
            elif check.kind == "contains_text" and check.text not in content:
                return False, self._failure(
                    index,
                    check,
                    f"required text is missing: {check.text}",
                )

        return True, f"{len(self._checks)}/{len(self._checks)} fixed checks passed"

    @staticmethod
    def _failure(index: int, check: StaticCheck, detail: str) -> str:
        return (
            f"check {index}/{check.kind}/{check.path} failed: {detail}"
        )


def _parse_function_case(value: object) -> FunctionCase:
    if type(value) is not dict or set(value) != {"args", "expected"}:
        raise ValueError("function case must contain exactly args and expected")
    if type(value["args"]) is not list:
        raise ValueError("function case args must be a list")
    return FunctionCase(tuple(value["args"]), value["expected"])


def _is_safe_scalar(value: object) -> bool:
    if value is None or type(value) in (bool, int, str):
        return True
    return type(value) is float and math.isfinite(value)


def _run_restricted_function_cases(
    content: str,
    check: StaticCheck,
) -> str | None:
    try:
        tree = ast.parse(content, filename=check.path)
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"
    except RecursionError:
        return "restricted function exceeds parser complexity limit"
    try:
        function = _load_restricted_function(tree, check.function)
    except ValueError as exc:
        return str(exc)

    for index, case in enumerate(check.cases, start=1):
        try:
            actual = function(*case.args)
        except Exception as exc:
            return f"case {index} raised {type(exc).__name__}"
        if type(actual) is not type(case.expected) or actual != case.expected:
            return f"case {index} returned an unexpected value"
    return None


def _load_restricted_function(tree: ast.Module, function_name: str):
    functions = [node for node in tree.body if type(node) is ast.FunctionDef]
    permitted_top_level = all(
        type(node) is ast.FunctionDef or _is_docstring_expression(node)
        for node in tree.body
    )
    if not permitted_top_level or len(functions) != 1:
        raise ValueError(
            "restricted check requires exactly one function definition; "
            "all other module-level statements must be string expressions"
        )
    function_node = functions[0]
    if function_node.name != function_name:
        raise ValueError("required function is missing")
    if function_node.name in RESTRICTED_FUNCTION_BUILTINS:
        raise ValueError("restricted function must not shadow a permitted built-in")
    if (
        function_node.decorator_list
        or getattr(function_node, "type_params", ())
        or function_node.returns is not None
        or function_node.type_comment is not None
        or function_node.args.posonlyargs
        or function_node.args.vararg is not None
        or function_node.args.kwonlyargs
        or function_node.args.kw_defaults
        or function_node.args.kwarg is not None
        or function_node.args.defaults
    ):
        raise ValueError(
            "restricted function uses unsupported signature or definition features; "
            "decorators, type parameters, return annotations, type comments, "
            "positional-only parameters, variadic parameters, keyword-only parameters "
            "and default arguments are not permitted"
        )
    if any(argument.annotation is not None for argument in function_node.args.args):
        raise ValueError("restricted function annotations are not supported")

    body = list(function_node.body)
    if body and _is_docstring_expression(body[0]):
        body = body[1:]
    if len(body) != 1 or type(body[0]) is not ast.Return:
        raise ValueError(
            "restricted function body must consist exclusively of one return statement; "
            "one leading string docstring is permitted; assignments and other "
            "statements are not permitted"
        )

    parameter_names = {argument.arg for argument in function_node.args.args}
    if any(name.startswith("_") for name in parameter_names):
        raise ValueError("restricted function has an unsafe parameter name")
    _validate_restricted_expression(body[0].value, parameter_names)
    expression_nodes = tuple(ast.walk(body[0].value))
    if len(expression_nodes) > RESTRICTED_EXPRESSION_NODES:
        raise ValueError("restricted function exceeds expression resource limit")
    # Check literals before compile can fold constant additions. Count every
    # occurrence, including inactive branches: this deliberately overestimates
    # both concatenations and the scalar text embedded in formatted strings.
    _check_restricted_value_budget(expression_nodes, None, parameter_names)

    safe_globals = {
        "__builtins__": {
            "bool": bool,
            "isinstance": isinstance,
            "str": str,
        }
    }
    exec(compile(tree, "<restricted-forge-check>", "exec"), safe_globals)
    compiled_function = safe_globals[function_name]
    argument_names = tuple(argument.arg for argument in function_node.args.args)

    def bounded_function(*args):
        if len(args) != len(argument_names):
            raise TypeError("restricted function argument count mismatch")
        for value in args:
            _restricted_scalar_cost(value)
        _check_restricted_value_budget(
            expression_nodes, dict(zip(argument_names, args)), parameter_names)
        return compiled_function(*args)

    return bounded_function


def _restricted_scalar_cost(value: object) -> int:
    if not _is_safe_scalar(value):
        raise ValueError("restricted function requires finite JSON scalars")
    if type(value) is str:
        if len(value) > RESTRICTED_VALUE_BUDGET:
            raise ValueError("restricted function exceeds value resource limit")
        return len(value)
    if type(value) is int:
        if value.bit_length() > RESTRICTED_INTEGER_BITS:
            raise ValueError("restricted function exceeds integer resource limit")
        return value.bit_length() + 2
    # Enough for None, bool and any finite float's default formatting.
    return 128


def _check_restricted_value_budget(
    nodes: tuple[ast.AST, ...],
    arguments: dict[str, object] | None,
    parameter_names: set[str],
) -> None:
    cost = 0
    for node in nodes:
        if type(node) is ast.Constant:
            cost += _restricted_scalar_cost(node.value)
        elif type(node) is ast.Name:
            if node.id in parameter_names:
                # None is the explicit literal-only pass before compilation.
                if arguments is not None:
                    if node.id not in arguments:
                        raise ValueError("restricted function has a missing resource argument")
                    cost += _restricted_scalar_cost(arguments[node.id])
            elif node.id in RESTRICTED_FUNCTION_BUILTINS:
                cost += 128
            else:
                raise ValueError("restricted function has an unknown resource name")
        elif type(node) in RESTRICTED_RESOURCE_FIXED_NODES:
            cost += 128
        elif type(node) not in RESTRICTED_RESOURCE_STRUCTURAL_NODES:
            raise ValueError("restricted function contains an unsupported resource node")
        if cost > RESTRICTED_VALUE_BUDGET:
            raise ValueError("restricted function exceeds value resource limit")


def _is_docstring_expression(node: ast.AST) -> bool:
    return (
        type(node) is ast.Expr
        and type(node.value) is ast.Constant
        and type(node.value.value) is str
    )


def _validate_restricted_expression(
    expression: ast.AST | None,
    parameter_names: set[str],
) -> None:
    if expression is None:
        raise ValueError("restricted function must return a value")
    allowed_types = (
        ast.Return,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Is,
        ast.IsNot,
        ast.In,
        ast.NotIn,
        ast.Call,
        ast.Attribute,
        ast.BinOp,
        ast.Add,
        ast.JoinedStr,
        ast.FormattedValue,
    )
    allowed_names = parameter_names | {"bool", "isinstance", "str"}
    for node in ast.walk(expression):
        if not isinstance(node, allowed_types):
            raise ValueError("restricted function contains an unsafe expression")
        if type(node) is ast.Name and node.id not in allowed_names:
            raise ValueError("restricted function uses an unknown name")
        if type(node) is ast.Attribute:
            if (
                node.attr != "strip"
                or type(node.value) is not ast.Name
                or node.value.id not in parameter_names
            ):
                raise ValueError("restricted function uses an unsafe attribute")
        if type(node) is ast.Call:
            _validate_restricted_call(node, parameter_names)
        if type(node) is ast.FormattedValue and (
            node.conversion != -1 or node.format_spec is not None
        ):
            raise ValueError("restricted function uses unsafe string formatting")


def _validate_restricted_call(
    call: ast.Call,
    parameter_names: set[str],
) -> None:
    if call.keywords:
        raise ValueError("restricted function calls may not use keywords")
    if type(call.func) is ast.Attribute:
        if (
            call.func.attr == "strip"
            and type(call.func.value) is ast.Name
            and call.func.value.id in parameter_names
            and not call.args
        ):
            return
        raise ValueError(
            "restricted function contains an unsafe method call: "
            f"line {call.lineno}, method {call.func.attr[:80]!r} "
            f"on {type(call.func.value).__name__}; "
            "only strip() directly on a function parameter, without arguments, "
            "is permitted; calls on another call's result are not permitted"
        )
    if type(call.func) is not ast.Name:
        raise ValueError("restricted function contains an unsafe call")
    if call.func.id in parameter_names:
        raise ValueError(
            "restricted function calls through function parameters are not permitted; "
            "this also applies when the called parameter has the same name as a built-in"
        )
    if call.func.id == "bool" and len(call.args) == 1:
        return
    if (
        call.func.id == "isinstance"
        and len(call.args) == 2
        and type(call.args[1]) is ast.Name
        and call.args[1].id == "str"
    ):
        if "str" in parameter_names:
            raise ValueError("restricted call must not shadow the str built-in with a parameter")
        return
    if call.func.id == "str":
        raise ValueError(
            "restricted function contains an unsafe call: "
            f"line {call.lineno}, str(...) is not permitted; "
            "use of str as the type name in isinstance(..., str) is permitted"
        )
    raise ValueError("restricted function contains an unsafe call")
