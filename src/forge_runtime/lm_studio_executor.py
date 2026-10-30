import json
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .contracts import SubagentResult, SubagentTask
from .handoff import build_role_handoff
from .result_parser import parse_subagent_result
from .conversation_protocol import CONVERSATION_SCHEMA
from .session_schema import SESSION_DRAFT_SCHEMA, SESSION_REVIEW_SCHEMA


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SYSTEM_PROMPT = (
    "You are one isolated Forge review-pipeline role. Use only the supplied "
    "handoff. Return exactly one JSON object with the fields role, passed, "
    "summary, and findings. Do not use Markdown or add other text."
)
_ACTION_SCHEMA = {
    "anyOf": [
        {
            "additionalProperties": False,
            "properties": {
                "action": {"const": "read_file"},
                "path": {"minLength": 1, "type": "string"},
            },
            "required": ["action", "path"],
            "type": "object",
        },
        {
            "additionalProperties": False,
            "properties": {
                "action": {"const": "write_file"},
                "content": {"type": "string"},
                "path": {"minLength": 1, "type": "string"},
            },
            "required": ["action", "path", "content"],
            "type": "object",
        },
        {
            "additionalProperties": False,
            "properties": {
                "action": {"const": "replace_text"},
                "new": {"type": "string"},
                "old": {"minLength": 1, "type": "string"},
                "path": {"minLength": 1, "type": "string"},
            },
            "required": ["action", "path", "old", "new"],
            "type": "object",
        },
        {
            "additionalProperties": False,
            "properties": {
                "action": {"const": "replace_text_batch"},
                "path": {"minLength": 1, "type": "string"},
                "replacements": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "new": {"type": "string"},
                            "old": {"minLength": 1, "type": "string"},
                        },
                        "required": ["old", "new"],
                        "type": "object",
                    },
                    "maxItems": 16,
                    "minItems": 1,
                    "type": "array",
                },
            },
            "required": ["action", "path", "replacements"],
            "type": "object",
        },
        {
            "additionalProperties": False,
            "properties": {"action": {"const": "run_check"}},
            "required": ["action"],
            "type": "object",
        },
        {
            "additionalProperties": False,
            "properties": {
                "action": {"const": "finish"},
                "findings": {
                    "items": {"minLength": 1, "type": "string"},
                    "type": "array",
                },
                "passed": {"type": "boolean"},
                "summary": {"minLength": 1, "type": "string"},
            },
            "required": ["action", "passed", "summary", "findings"],
            "type": "object",
        },
    ]
}
_ACTION_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "replace_text",
        "replace_text_batch",
        "run_check",
        "finish",
    }
)
_RESULT_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "findings": {
            "items": {"minLength": 1, "type": "string"},
            "type": "array",
        },
        "passed": {"type": "boolean"},
        "role": {"enum": ["builder", "tester", "reviewer"], "type": "string"},
        "summary": {"minLength": 1, "type": "string"},
    },
    "required": ["role", "passed", "summary", "findings"],
    "type": "object",
}


class LMStudioChatClient:
    """Send one validated conversation to a local LM Studio endpoint."""

    def __init__(
        self,
        model: str,
        endpoint: str = "http://127.0.0.1:1234/v1/chat/completions",
        timeout_seconds: float = 120.0,
        opener=urlopen,
        response_kind: str = "tool_action",
        execution_copy_field_order: bool = False,
    ):
        if type(model) is not str or not model.strip():
            raise ValueError("model must be a non-empty string")
        if type(endpoint) is not str:
            raise ValueError("endpoint must be a string")
        parsed_endpoint = urlparse(endpoint)
        if (
            parsed_endpoint.scheme != "http"
            or parsed_endpoint.hostname not in _LOCAL_HOSTS
            or parsed_endpoint.path != "/v1/chat/completions"
            or parsed_endpoint.params
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ValueError("endpoint must be a local LM Studio chat-completions URL")
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        if not callable(opener):
            raise ValueError("opener must be callable")
        if type(response_kind) is not str or response_kind not in (
            "tool_action",
            "subagent_result",
            "conversation",
            "handoff_draft",
            "handoff_review",
        ):
            raise ValueError("Unsupported response_kind")

        if type(execution_copy_field_order) is not bool or (
            execution_copy_field_order and response_kind != "tool_action"
        ):
            raise ValueError("execution copy field order requires tool_action and a boolean")
        self.execution_copy_field_order = execution_copy_field_order
        self.model = model
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener
        self.response_kind = response_kind
        self._tool_actions = _ACTION_NAMES

    def set_tool_actions(self, actions: tuple[str, ...]) -> None:
        if (
            self.response_kind != "tool_action"
            or type(actions) is not tuple
            or not actions
            or any(type(action) is not str for action in actions)
            or len(set(actions)) != len(actions)
            or not set(actions).issubset(_ACTION_NAMES)
            or "finish" not in actions
        ):
            raise ValueError("tool actions must be unique registered names including finish")
        self._tool_actions = frozenset(actions)

    def __call__(self, messages: tuple[tuple[str, str], ...]) -> str:
        if type(messages) is not tuple or not messages:
            raise ValueError("messages must be a non-empty tuple")
        payload_messages = []
        for message in messages:
            if type(message) is not tuple or len(message) != 2:
                raise ValueError("each message must be a role-content tuple")
            role, content = message
            if type(role) is not str or role not in ("system", "user", "assistant"):
                raise ValueError("message role must be system, user, or assistant")
            if type(content) is not str or not content:
                raise ValueError("message content must be a non-empty string")
            payload_messages.append({"role": role, "content": content})

        schema = (
            _tool_action_schema(self._tool_actions)
            if self.response_kind == "tool_action"
            else _RESULT_SCHEMA
        )
        if self.response_kind == "conversation":
            schema = CONVERSATION_SCHEMA
        elif self.response_kind == "handoff_draft":
            schema = SESSION_DRAFT_SCHEMA
        elif self.response_kind == "handoff_review":
            schema = SESSION_REVIEW_SCHEMA
        payload = {
            "messages": payload_messages,
            "model": self.model,
            "response_format": {
                "json_schema": {
                    "name": self.response_kind,
                    "schema": schema,
                    "strict": True,
                },
                "type": "json_schema",
            },
            "stream": False,
            "temperature": 0,
        }
        if self.execution_copy_field_order:
            # Retain canonical ordering everywhere except the two copy mappings.
            payload = json.loads(json.dumps(payload, sort_keys=True))
            for action_schema in payload["response_format"]["json_schema"]["schema"]["anyOf"]:
                properties = action_schema["properties"]
                order = {
                    "write_file": ("action", "path", "content"),
                    "replace_text": ("action", "path", "old", "new"),
                }.get(properties["action"]["const"])
                if order is not None:
                    action_schema["properties"] = {key: properties[key] for key in order}
        request = Request(
            self.endpoint,
            data=json.dumps(
                payload,
                ensure_ascii=True,
                # Preserve reply-before-questions in the conversation grammar.
                # Execution opt-in changes only the two copy property mappings.
                sort_keys=(self.response_kind != "conversation"
                           and not self.execution_copy_field_order),
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                if self.response_kind in {"handoff_draft", "handoff_review"}:
                    raw_response = response.read(100_001)
                    if len(raw_response) > 100_000:
                        raise RuntimeError("Handoff model response exceeds byte limit")
                else:
                    raw_response = response.read()
        except (OSError, TimeoutError) as exc:
            raise RuntimeError("local LM Studio request failed") from exc

        try:
            response_data = json.loads(raw_response.decode("utf-8"))
            content = response_data["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("local LM Studio returned an invalid response") from exc
        if type(content) is not str or not content:
            raise RuntimeError("local LM Studio returned invalid message content")
        return content


def _tool_action_schema(action_names: frozenset[str]) -> dict[str, object]:
    return {
        "anyOf": [
            schema
            for schema in _ACTION_SCHEMA["anyOf"]
            if schema["properties"]["action"]["const"] in action_names
        ]
    }


class LMStudioExecutor:
    """Execute one final-result Forge role through local LM Studio."""

    def __init__(
        self,
        model: str,
        endpoint: str = "http://127.0.0.1:1234/v1/chat/completions",
        timeout_seconds: float = 120.0,
        opener=urlopen,
    ):
        self._client = LMStudioChatClient(
            model=model,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
            opener=opener,
            response_kind="subagent_result",
        )
        self.model = self._client.model
        self.endpoint = self._client.endpoint
        self.timeout_seconds = self._client.timeout_seconds

    def __call__(
        self,
        role: str,
        task: SubagentTask,
        previous_results: tuple[SubagentResult, ...],
    ) -> SubagentResult:
        handoff = build_role_handoff(role, task, previous_results)
        content = self._client(
            (("system", _SYSTEM_PROMPT), ("user", handoff))
        )
        return parse_subagent_result(role, content)
