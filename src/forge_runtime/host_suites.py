"""Fixed host-side suites for agent structures too large for scalar checks."""

import ast
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Callable, Mapping

from .advanced_policy_suite import run_advanced_defensive_policy
from .audited_decision_suite import run_audited_decision_gate
from .audited_config_analysis_suite import run_audited_config_analysis
from .audit_suite import run_minimized_audit_chain
from .jsonl_audit_suite import run_jsonl_audit_log
from .config_analysis_suite import run_redacted_config_analysis
from .foundation_suite import run_defensive_policy_foundation
from .recovery_suite import run_audit_recovery
from .audit_acl_suite import run_audit_acl
from .durable_audit_suite import run_durable_audit
from .partitioned_audit_suite import (
    ACL_PART_FILES, AUDIT_PART_FILES,
    run_partitioned_audit_acl, run_partitioned_durable_audit,
)
from .workspace_tools import WorkspaceFileTools
from .policy_service_suite import REQUIRED_FILES as POLICY_SERVICE_FILES, run_policy_service
from .capability_registry_suite import REQUIRED_FILES as CAPABILITY_REGISTRY_FILES, run_capability_registry
from .execution_contract_suite import REQUIRED_FILES as EXECUTION_CONTRACT_FILES, run_execution_contract
from .active_execution_suite import REQUIRED_FILES as ACTIVE_EXECUTION_FILES, run_active_execution
from .f001_file_analyzer_suite import REQUIRED_FILES as F001_FILES, run_f001_file_analyzer


SuiteRunner = Callable[[Mapping[str, str]], str | None]


@dataclass(frozen=True, slots=True)
class HostSuiteDefinition:
    """An immutable binding between a suite name, its files, and host code."""

    identifier: str
    required_files: tuple[str, ...]
    runner: SuiteRunner

    def __post_init__(self) -> None:
        if type(self.identifier) is not str or not self.identifier:
            raise ValueError("host suite identifier must be a non-empty string")
        if type(self.required_files) is not tuple or not self.required_files:
            raise ValueError("host suite required_files must be a non-empty tuple")
        if any(type(path) is not str or not path for path in self.required_files):
            raise ValueError("host suite file paths must be non-empty strings")
        if len(set(self.required_files)) != len(self.required_files):
            raise ValueError("host suite file paths must be unique")
        if not callable(self.runner):
            raise ValueError("host suite runner must be callable")


def required_files_for_suite(identifier: str) -> tuple[str, ...]:
    """Return the exact file contract of a registered suite."""
    try:
        return HOST_SUITE_REGISTRY[identifier].required_files
    except (KeyError, TypeError) as exc:
        raise ValueError("host suite is not registered") from exc


def run_host_suite(
    identifier: str,
    workspace: WorkspaceFileTools,
) -> tuple[bool, str]:
    """Read suite inputs through the workspace gate and run fixed host code."""
    if not isinstance(workspace, WorkspaceFileTools):
        raise ValueError("workspace must be WorkspaceFileTools")
    try:
        definition = HOST_SUITE_REGISTRY[identifier]
    except (KeyError, TypeError) as exc:
        raise ValueError("host suite is not registered") from exc

    contents: dict[str, str] = {}
    for path in definition.required_files:
        try:
            contents[path] = workspace.read_text(path)
        except FileNotFoundError:
            return False, f"required file is missing: {path}"
    failure = definition.runner(MappingProxyType(contents))
    if failure is not None:
        return False, failure
    return True, f"host suite {identifier} passed"


def _run_two_module_policy(contents: Mapping[str, str]) -> str | None:
    try:
        registry_tree = ast.parse(
            contents["agent/registry.py"], filename="agent/registry.py"
        )
        policy_tree = ast.parse(
            contents["agent/policy.py"], filename="agent/policy.py"
        )
    except SyntaxError as exc:
        return f"invalid Python syntax at line {exc.lineno or 0}"

    registry_failure = _validate_registry_module(registry_tree)
    if registry_failure is not None:
        return registry_failure
    policy_failure = _validate_policy_module(policy_tree)
    if policy_failure is not None:
        return policy_failure
    return _run_policy_cases(policy_tree)


def _module_body_without_docstring(tree: ast.Module) -> list[ast.stmt]:
    body = list(tree.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    return body


def _is_docstring(node: ast.AST) -> bool:
    return (
        type(node) is ast.Expr
        and type(node.value) is ast.Constant
        and type(node.value.value) is str
    )


def _validate_registry_module(tree: ast.Module) -> str | None:
    body = _module_body_without_docstring(tree)
    imports = [node for node in body if type(node) is ast.ImportFrom]
    classes = [node for node in body if type(node) is ast.ClassDef]
    assignments = [node for node in body if type(node) is ast.Assign]
    if (
        len(body) != 6
        or len(imports) != 3
        or len(classes) != 2
        or len(assignments) != 1
    ):
        return "registry must contain only three imports, two classes, and one registry"

    expected_imports = {
        ("dataclasses", "dataclass"),
        ("enum", "Enum"),
        ("types", "MappingProxyType"),
    }
    actual_imports: set[tuple[str | None, str]] = set()
    for node in imports:
        if node.level != 0 or len(node.names) != 1 or node.names[0].asname is not None:
            return "registry imports must use the exact public names"
        actual_imports.add((node.module, node.names[0].name))
    if actual_imports != expected_imports:
        return "registry imports do not match the fixed contract"

    classes_by_name = {node.name: node for node in classes}
    if set(classes_by_name) != {"SecurityClass", "CapabilityDefinition"}:
        return "registry classes do not match the fixed contract"
    failure = _validate_security_class(classes_by_name["SecurityClass"])
    if failure is not None:
        return failure
    failure = _validate_capability_definition(classes_by_name["CapabilityDefinition"])
    if failure is not None:
        return failure
    return _validate_capability_registry(assignments[0])


def _validate_security_class(node: ast.ClassDef) -> str | None:
    if (
        node.decorator_list
        or node.keywords
        or len(node.bases) != 1
        or type(node.bases[0]) is not ast.Name
        or node.bases[0].id != "Enum"
    ):
        return "SecurityClass must directly extend Enum"
    body = list(node.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    expected = [("PASSIVE", "passive"), ("PROHIBITED", "prohibited")]
    actual: list[tuple[str, str]] = []
    for statement in body:
        if (
            type(statement) is not ast.Assign
            or len(statement.targets) != 1
            or type(statement.targets[0]) is not ast.Name
            or type(statement.value) is not ast.Constant
            or type(statement.value.value) is not str
        ):
            return "SecurityClass may contain only the two fixed values"
        actual.append((statement.targets[0].id, statement.value.value))
    if actual != expected:
        return "SecurityClass values do not match the fixed contract"
    return None


def _validate_capability_definition(node: ast.ClassDef) -> str | None:
    if node.bases or node.keywords or len(node.decorator_list) != 1:
        return "CapabilityDefinition must be a frozen slots dataclass"
    decorator = node.decorator_list[0]
    decorator_options = (
        {
            keyword.arg: keyword.value.value
            for keyword in decorator.keywords
            if (
                keyword.arg is not None
                and type(keyword.value) is ast.Constant
                and type(keyword.value.value) is bool
            )
        }
        if type(decorator) is ast.Call
        else {}
    )
    if (
        type(decorator) is not ast.Call
        or type(decorator.func) is not ast.Name
        or decorator.func.id != "dataclass"
        or decorator.args
        or len(decorator.keywords) != 2
        or decorator_options != {"frozen": True, "slots": True}
    ):
        return "CapabilityDefinition must be a frozen slots dataclass"
    body = list(node.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    expected = [("identifier", "str"), ("security_class", "SecurityClass")]
    actual: list[tuple[str, str]] = []
    for statement in body:
        if (
            type(statement) is not ast.AnnAssign
            or statement.value is not None
            or statement.simple != 1
            or type(statement.target) is not ast.Name
            or type(statement.annotation) is not ast.Name
        ):
            return "CapabilityDefinition fields do not match the fixed contract"
        actual.append((statement.target.id, statement.annotation.id))
    if actual != expected:
        return "CapabilityDefinition fields do not match the fixed contract"
    return None


def _validate_capability_registry(node: ast.Assign) -> str | None:
    if (
        len(node.targets) != 1
        or type(node.targets[0]) is not ast.Name
        or node.targets[0].id != "CAPABILITY_REGISTRY"
        or type(node.value) is not ast.Call
        or type(node.value.func) is not ast.Name
        or node.value.func.id != "MappingProxyType"
        or len(node.value.args) != 1
        or node.value.keywords
        or type(node.value.args[0]) is not ast.Dict
    ):
        return "CAPABILITY_REGISTRY must be an immutable MappingProxyType"
    mapping = node.value.args[0]
    expected = {
        "explain_security_concepts": "PASSIVE",
        "obtain_credentials": "PROHIBITED",
    }
    actual: dict[str, str] = {}
    for key, value in zip(mapping.keys, mapping.values):
        if type(key) is not ast.Constant or type(key.value) is not str:
            return "capability registry keys must be fixed strings"
        if key.value in actual:
            return "capability registry keys must be unique"
        if (
            type(value) is not ast.Call
            or type(value.func) is not ast.Name
            or value.func.id != "CapabilityDefinition"
        ):
            return "capability registry entries do not match the fixed contract"
        entry_arguments = _capability_entry_arguments(value)
        if entry_arguments is None:
            return "capability registry entries do not match the fixed contract"
        identifier, security_class = entry_arguments
        if (
            type(identifier) is not ast.Constant
            or identifier.value != key.value
            or type(security_class) is not ast.Attribute
            or type(security_class.value) is not ast.Name
            or security_class.value.id != "SecurityClass"
        ):
            return "capability registry entries do not match the fixed contract"
        actual[key.value] = security_class.attr
    if actual != expected:
        return "capability registry entries do not match the fixed contract"
    return None


def _capability_entry_arguments(
    call: ast.Call,
) -> tuple[ast.expr, ast.expr] | None:
    if len(call.args) == 2 and not call.keywords:
        return call.args[0], call.args[1]
    if call.args or len(call.keywords) != 2:
        return None
    if [keyword.arg for keyword in call.keywords] != [
        "identifier",
        "security_class",
    ]:
        return None
    return call.keywords[0].value, call.keywords[1].value


def _validate_policy_module(tree: ast.Module) -> str | None:
    body = _module_body_without_docstring(tree)
    if (
        len(body) != 2
        or type(body[0]) is not ast.ImportFrom
        or type(body[1]) is not ast.FunctionDef
    ):
        return "policy must contain only the fixed import and assess_capability"
    imported = body[0]
    if (
        imported.level != 1
        or imported.module != "registry"
        or [(alias.name, alias.asname) for alias in imported.names]
        != [("CAPABILITY_REGISTRY", None), ("SecurityClass", None)]
    ):
        return "policy import does not match the fixed contract"

    function = body[1]
    if (
        function.name != "assess_capability"
        or function.decorator_list
        or function.returns is not None
        or function.type_comment is not None
        or function.args.posonlyargs
        or function.args.vararg is not None
        or function.args.kwonlyargs
        or function.args.kw_defaults
        or function.args.kwarg is not None
        or function.args.defaults
        or len(function.args.args) != 1
        or function.args.args[0].arg != "capability"
        or function.args.args[0].annotation is not None
    ):
        return "assess_capability signature does not match the fixed contract"

    statements = list(function.body)
    if statements and _is_docstring(statements[0]):
        statements = statements[1:]
    if (
        len(statements) != 4
        or not _is_registry_lookup(statements[0])
        or not _is_returning_if(statements[1], "definition", None, "needs_clarification")
        or not _is_prohibited_if(statements[2])
        or not _is_string_return(statements[3], "approved")
    ):
        return "assess_capability body does not match the fixed safe structure"
    return None


def _is_registry_lookup(node: ast.AST) -> bool:
    return (
        type(node) is ast.Assign
        and len(node.targets) == 1
        and type(node.targets[0]) is ast.Name
        and node.targets[0].id == "definition"
        and type(node.value) is ast.Call
        and type(node.value.func) is ast.Attribute
        and type(node.value.func.value) is ast.Name
        and node.value.func.value.id == "CAPABILITY_REGISTRY"
        and node.value.func.attr == "get"
        and len(node.value.args) == 1
        and type(node.value.args[0]) is ast.Name
        and node.value.args[0].id == "capability"
        and not node.value.keywords
    )


def _is_returning_if(
    node: ast.AST,
    left_name: str,
    right_value: object,
    returned: str,
) -> bool:
    return (
        type(node) is ast.If
        and not node.orelse
        and len(node.body) == 1
        and type(node.test) is ast.Compare
        and type(node.test.left) is ast.Name
        and node.test.left.id == left_name
        and len(node.test.ops) == 1
        and type(node.test.ops[0]) is ast.Is
        and len(node.test.comparators) == 1
        and type(node.test.comparators[0]) is ast.Constant
        and node.test.comparators[0].value is right_value
        and _is_string_return(node.body[0], returned)
    )


def _is_prohibited_if(node: ast.AST) -> bool:
    if type(node) is not ast.If or node.orelse or len(node.body) != 1:
        return False
    test = node.test
    return (
        type(test) is ast.Compare
        and type(test.left) is ast.Attribute
        and type(test.left.value) is ast.Name
        and test.left.value.id == "definition"
        and test.left.attr == "security_class"
        and len(test.ops) == 1
        and type(test.ops[0]) is ast.Is
        and len(test.comparators) == 1
        and type(test.comparators[0]) is ast.Attribute
        and type(test.comparators[0].value) is ast.Name
        and test.comparators[0].value.id == "SecurityClass"
        and test.comparators[0].attr == "PROHIBITED"
        and _is_string_return(node.body[0], "rejected")
    )


def _is_string_return(node: ast.AST, expected: str) -> bool:
    return (
        type(node) is ast.Return
        and type(node.value) is ast.Constant
        and node.value.value == expected
    )


def _run_policy_cases(tree: ast.Module) -> str | None:
    function = _module_body_without_docstring(tree)[1]
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    passive = object()
    prohibited = object()
    safe_globals = {
        "__builtins__": {},
        "CAPABILITY_REGISTRY": MappingProxyType(
            {
                "explain_security_concepts": SimpleNamespace(
                    security_class=passive
                ),
                "obtain_credentials": SimpleNamespace(
                    security_class=prohibited
                ),
            }
        ),
        "SecurityClass": SimpleNamespace(PROHIBITED=prohibited),
    }
    exec(compile(module, "<forge-host-suite>", "exec"), safe_globals)
    function_object = safe_globals["assess_capability"]
    cases = (
        ("explain_security_concepts", "approved"),
        ("obtain_credentials", "rejected"),
        ("unknown_capability", "needs_clarification"),
    )
    for capability, expected in cases:
        try:
            actual = function_object(capability)
        except Exception as exc:
            return f"policy case raised {type(exc).__name__}"
        if type(actual) is not str or actual != expected:
            return f"policy returned an unexpected result for {capability}"
    return None


_TWO_MODULE_POLICY = HostSuiteDefinition(
    identifier="two_module_policy_v1",
    required_files=("agent/registry.py", "agent/policy.py"),
    runner=_run_two_module_policy,
)

_DEFENSIVE_POLICY_FOUNDATION = HostSuiteDefinition(
    identifier="defensive_policy_foundation_v1",
    required_files=(
        "agent/capabilities.py",
        "agent/decisions.py",
        "agent/policy.py",
    ),
    runner=run_defensive_policy_foundation,
)

_ADVANCED_DEFENSIVE_POLICY = HostSuiteDefinition(
    identifier="advanced_defensive_policy_v1",
    required_files=(
        "agent/capabilities.py",
        "agent/decisions.py",
        "agent/policy.py",
    ),
    runner=run_advanced_defensive_policy,
)

_REDACTED_CONFIG_ANALYSIS = HostSuiteDefinition(
    identifier="redacted_config_analysis_v1",
    required_files=("agent/config_analyzer.py",),
    runner=run_redacted_config_analysis,
)

_MINIMIZED_AUDIT_CHAIN = HostSuiteDefinition(
    identifier="minimized_audit_chain_v1",
    required_files=("agent/audit_chain.py",),
    runner=run_minimized_audit_chain,
)

_JSONL_AUDIT_LOG = HostSuiteDefinition(
    identifier="jsonl_audit_log_v1",
    required_files=("agent/audit_chain.py", "agent/audit_log.py"),
    runner=run_jsonl_audit_log,
)

_AUDITED_DECISION_GATE = HostSuiteDefinition(
    identifier="audited_decision_gate_v1",
    required_files=("agent/decision_gate.py",),
    runner=run_audited_decision_gate,
)

_AUDITED_CONFIG_ANALYSIS = HostSuiteDefinition(
    identifier="audited_config_analysis_v1",
    required_files=("agent/config_analysis_service.py",),
    runner=run_audited_config_analysis,
)

_AUDIT_RECOVERY = HostSuiteDefinition(
    identifier="audit_recovery_v1",
    required_files=("agent/audit_recovery.py",),
    runner=run_audit_recovery,
)

_AUDIT_ACL = HostSuiteDefinition(
    identifier="audit_acl_v1",
    required_files=("agent/audit_acl.py",),
    runner=run_audit_acl,
)

_DURABLE_AUDIT = HostSuiteDefinition(
    identifier="durable_audit_v1",
    required_files=("agent/audit.py", "agent/audit_acl.py"),
    runner=run_durable_audit,
)

_PARTITIONED_AUDIT_ACL = HostSuiteDefinition(
    identifier="partitioned_audit_acl_v1",
    required_files=ACL_PART_FILES,
    runner=run_partitioned_audit_acl,
)

_PARTITIONED_DURABLE_AUDIT = HostSuiteDefinition(
    identifier="partitioned_durable_audit_v1",
    required_files=AUDIT_PART_FILES + ("agent/audit_acl.py",),
    runner=run_partitioned_durable_audit,
)

_POLICY_SERVICE = HostSuiteDefinition(
    identifier="policy_service_v1",
    required_files=POLICY_SERVICE_FILES,
    runner=run_policy_service,
)

_CAPABILITY_REGISTRY = HostSuiteDefinition(
    identifier="capability_registry_v1",
    required_files=CAPABILITY_REGISTRY_FILES,
    runner=run_capability_registry,
)

_EXECUTION_CONTRACT = HostSuiteDefinition(
    identifier="execution_contract_v1",
    required_files=EXECUTION_CONTRACT_FILES,
    runner=run_execution_contract,
)

_ACTIVE_EXECUTION = HostSuiteDefinition(
    identifier="active_execution_v1",
    required_files=ACTIVE_EXECUTION_FILES,
    runner=run_active_execution,
)

_F001_FILE_ANALYZER = HostSuiteDefinition(
    identifier="f001_file_analyzer_v1",
    required_files=F001_FILES,
    runner=run_f001_file_analyzer,
)

HOST_SUITE_REGISTRY = MappingProxyType(
    {
        _TWO_MODULE_POLICY.identifier: _TWO_MODULE_POLICY,
        _DEFENSIVE_POLICY_FOUNDATION.identifier: _DEFENSIVE_POLICY_FOUNDATION,
        _ADVANCED_DEFENSIVE_POLICY.identifier: _ADVANCED_DEFENSIVE_POLICY,
        _REDACTED_CONFIG_ANALYSIS.identifier: _REDACTED_CONFIG_ANALYSIS,
        _MINIMIZED_AUDIT_CHAIN.identifier: _MINIMIZED_AUDIT_CHAIN,
        _JSONL_AUDIT_LOG.identifier: _JSONL_AUDIT_LOG,
        _AUDITED_DECISION_GATE.identifier: _AUDITED_DECISION_GATE,
        _AUDITED_CONFIG_ANALYSIS.identifier: _AUDITED_CONFIG_ANALYSIS,
        _AUDIT_RECOVERY.identifier: _AUDIT_RECOVERY,
        _AUDIT_ACL.identifier: _AUDIT_ACL,
        _DURABLE_AUDIT.identifier: _DURABLE_AUDIT,
        _PARTITIONED_AUDIT_ACL.identifier: _PARTITIONED_AUDIT_ACL,
        _PARTITIONED_DURABLE_AUDIT.identifier: _PARTITIONED_DURABLE_AUDIT,
        _POLICY_SERVICE.identifier: _POLICY_SERVICE,
        _CAPABILITY_REGISTRY.identifier: _CAPABILITY_REGISTRY,
        _EXECUTION_CONTRACT.identifier: _EXECUTION_CONTRACT,
        _ACTIVE_EXECUTION.identifier: _ACTIVE_EXECUTION,
        _F001_FILE_ANALYZER.identifier: _F001_FILE_ANALYZER,
    }
)
