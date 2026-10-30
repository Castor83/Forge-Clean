"""Explicit local Ollama lifecycle for the already verified Forge model."""

import json
from contextlib import contextmanager
from io import BytesIO
from urllib.request import Request, urlopen

from .model_manager import ModelManager, ModelSelection, ProviderProfile


PROFILE = ProviderProfile(
    profile_id="ollama-qwen25-coder14b-4k",
    provider_id="ollama",
    model_id="huihui_ai/qwen2.5-coder-abliterate:14b",
    endpoint="http://127.0.0.1:11434/v1/chat/completions",
    context_length=4096,
    parallel=1,
    temperature=0,
    model_identity="GGUF SHA256 831064078d831082231a66cc7e3b9db8adaf2dac574cc51363c08b7dbd408ff3",
)
_MANIFEST = "ac9889decebb11a8bd5bddcc52553843f521fd87f1d8d210e4b6cfe5da77f522"
_BASE = "http://127.0.0.1:11434"


class OllamaRuntime:
    """No downloads, process control, retries, or automatic model replacement."""

    def __init__(self, *, opener=urlopen):
        if not callable(opener):
            raise ValueError("Ollama transport must be callable")
        self._opener = opener
        self.manager = ModelManager((PROFILE,))
        self.selection = ModelSelection("ollama", PROFILE.model_id)

    def _request(self, path, body=None):
        request = Request(
            _BASE + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with self._opener(request, timeout=120) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Ollama response exceeds the byte limit")
            data = json.loads(raw.decode("utf-8"))
            if type(data) is not dict or "error" in data:
                raise ValueError("Invalid Ollama response")
            return data
        except (OSError, TimeoutError) as exc:
            raise RuntimeError("Ollama ist nicht erreichbar. Bitte die vorhandene Ollama-App starten.") from exc

    def status(self):
        version = self._request("/api/version")
        tags = self._request("/api/tags")
        running = self._request("/api/ps")
        if (type(version.get("version")) is not str
                or type(tags.get("models")) is not list
                or type(running.get("models")) is not list
                or any(type(item) is not dict for item in tags["models"] + running["models"])):
            raise ValueError("Ollama hat einen ungueltigen Modellstatus geliefert.")
        return {"backend_version": version["version"],
                "installed_models": tags["models"], "loaded_models": running["models"],
                "model_id": PROFILE.model_id, "required_context": PROFILE.context_length}

    def _ready(self, state):
        catalog = [item for item in state["installed_models"] if item.get("name") == PROFILE.model_id]
        if len(catalog) != 1:
            raise ValueError("Das ausgewaehlte Ollama-Modell ist nicht installiert. Kein Download gestartet.")
        if catalog[0].get("digest") != _MANIFEST:
            raise ValueError("Das installierte Modell weicht von der geprueften Version ab.")
        loaded = state["loaded_models"]
        if any(item.get("name") != PROFILE.model_id for item in loaded):
            raise ValueError("Ein anderes Ollama-Modell ist geladen. Es wird nicht ersetzt.")
        if not loaded:
            raise ValueError("Forge-Modell ist entladen. Zuerst Ollama-Start verwenden.")
        if len(loaded) != 1 or loaded[0].get("digest") != _MANIFEST:
            raise ValueError("Die geladene Modellversion passt nicht zum Forge-Profil.")
        if loaded[0].get("context_length") != PROFILE.context_length:
            raise ValueError("Das Modell hat ein anderes Kontextfenster. Kein stiller Modellwechsel.")
        return {**state, "loaded_model": loaded[0]}

    def start(self):
        state = self.status()
        if state["loaded_models"]:
            return self._ready(state)
        catalog = [item for item in state["installed_models"] if item.get("name") == PROFILE.model_id]
        if len(catalog) != 1 or catalog[0].get("digest") != _MANIFEST:
            raise ValueError("Das gepruefte Ollama-Modell ist nicht installiert. Kein Download gestartet.")
        result = self._request("/api/generate", {
            "model": PROFILE.model_id, "prompt": "", "stream": False,
            "keep_alive": "10m", "options": {"num_ctx": PROFILE.context_length,
                "temperature": 0, "seed": 42, "num_batch": 512},
        })
        if result.get("done") is not True or result.get("done_reason") != "load":
            raise ValueError("Ollama hat die Modellladung nicht bestaetigt.")
        return self._ready(self.status())

    def require_ready(self):
        return self._ready(self.status())

    @contextmanager
    def _guarded_chat_opener(self, request, timeout):
        try:
            payload = json.loads(request.data.decode("utf-8"))
            schema = payload["response_format"]["json_schema"]["schema"]
        except (AttributeError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("Invalid Forge chat request") from exc
        if (request.full_url != PROFILE.endpoint or payload.get("model") != PROFILE.model_id
                or type(schema) is not dict):
            raise ValueError("Ollama chat request differs from the registered Forge profile")
        native_request = Request(
            _BASE + "/api/chat",
            data=json.dumps({
                "model": PROFILE.model_id, "messages": payload["messages"],
                "stream": False, "format": schema, "keep_alive": "10m",
                "options": {"num_ctx": PROFILE.context_length, "temperature": PROFILE.temperature,
                            "seed": 42, "num_batch": 512},
            }, ensure_ascii=True).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        self.require_ready()
        with self._opener(native_request, timeout=timeout) as response:
            raw = response.read(1_000_001)
        self.require_ready()
        if len(raw) > 1_000_000:
            raise ValueError("Ollama chat response exceeds the byte limit")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid Ollama chat response") from exc
        if (type(data) is not dict or "error" in data or data.get("done") is not True
                or data.get("done_reason") != "stop" or data.get("model") != PROFILE.model_id):
            raise ValueError("Ollama chat response is incomplete or invalid")
        message = data.get("message")
        if (type(message) is not dict or message.get("role") != "assistant"
                or type(message.get("content")) is not str or not message["content"]):
            raise ValueError("Ollama chat returned an invalid message")
        converted = {"choices": [{"message": {"role": "assistant", "content": message["content"]},
                                  "finish_reason": "stop"}]}
        with BytesIO(json.dumps(converted, ensure_ascii=True).encode("utf-8")) as response:
            yield response

    def create_chat_client(self, *, response_kind="tool_action", timeout_seconds=120,
                           execution_copy_field_order=False):
        client = self.manager.create_chat_client(
            self.selection, response_kind=response_kind,
            timeout_seconds=timeout_seconds, opener=self._guarded_chat_opener,
            execution_copy_field_order=execution_copy_field_order,
        )
        self.require_ready()
        return client

    def chat_factory(self, model, endpoint=PROFILE.endpoint, timeout_seconds=120, *,
                     response_kind="tool_action", execution_copy_field_order=False):
        if model != PROFILE.model_id or endpoint != PROFILE.endpoint:
            raise ValueError("Ollama client must use the registered Forge model and endpoint")
        return self.create_chat_client(
            response_kind=response_kind, timeout_seconds=timeout_seconds,
            execution_copy_field_order=execution_copy_field_order,
        )

    def stop(self):
        running = self._request("/api/ps")
        if type(running.get("models")) is not list:
            raise ValueError("Ollama hat einen ungueltigen Modellstatus geliefert.")
        if not any(item.get("name") == PROFILE.model_id for item in running["models"]):
            return {"done": True, "done_reason": "already_unloaded"}
        return self._request("/api/generate", {
            "model": PROFILE.model_id, "prompt": "", "stream": False, "keep_alive": 0,
        })
