"""Host-owned, offline provider registry with explicit model selection.

Profiles describe required settings, not observed server settings. Evidence is a
host-supplied declaration bound to one model; this module neither verifies its
contents nor promotes its status. Selection and client construction do no I/O.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from urllib.parse import urlparse
from urllib.request import urlopen

from .lm_studio_executor import LMStudioChatClient


_PROVIDERS = frozenset({"lmstudio", "ollama"})
_EVIDENCE_STATUSES = frozenset({
    "offline_contract_passed", "loaded", "schema_smoke_passed",
    "functional_build_passed",
})


def _text(value: str, name: str) -> None:
    if (type(value) is not str or not value or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{name} must be non-empty text without surrounding whitespace or controls")


def _selection(provider_id: str, model_id: str) -> None:
    if type(provider_id) is not str or provider_id not in _PROVIDERS:
        raise ValueError("provider_id must be a registered local provider")
    _text(model_id, "model_id")
    if any(char.isspace() for char in model_id):
        raise ValueError("model_id must not contain whitespace")


@dataclass(frozen=True)
class ModelSelection:
    provider_id: str
    model_id: str

    def __post_init__(self):
        _selection(self.provider_id, self.model_id)


@dataclass(frozen=True)
class ProviderEvidence:
    status: str
    backend_version: str
    model_identity: str
    test_contract: str
    observed_at: datetime
    evidence_path: str
    profile_fingerprint: str
    model_digest: str
    model_digest_kind: str

    def __post_init__(self):
        if type(self.status) is not str or self.status not in _EVIDENCE_STATUSES:
            raise ValueError("unsupported evidence status")
        for value in (self.profile_fingerprint, self.model_digest):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("profile fingerprint and model digest must be SHA256 hex strings")
        if (type(self.model_digest_kind) is not str or self.model_digest_kind not in {
                "gguf_sha256", "ollama_manifest_sha256", "offline_fixture_sha256"}):
            raise ValueError("model digest must distinguish file, manifest and offline fixture")
        if self.model_digest_kind == "offline_fixture_sha256" and self.status != "offline_contract_passed":
            raise ValueError("offline fixture evidence cannot claim a live model success")
        for name in ("backend_version", "model_identity", "test_contract", "evidence_path"):
            _text(getattr(self, name), name)
        if (type(self.observed_at) is not datetime or self.observed_at.tzinfo is None
                or self.observed_at.utcoffset() != timedelta(0)):
            raise ValueError("observed_at must be a UTC datetime")
        path = PurePosixPath(self.evidence_path)
        if (path.is_absolute() or "\\" in self.evidence_path or ":" in self.evidence_path
                or any(part in {"", ".", ".."} for part in self.evidence_path.split("/"))):
            raise ValueError("evidence_path must be a relative repository path")


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    provider_id: str
    model_id: str
    endpoint: str
    context_length: int
    parallel: int
    temperature: int
    model_identity: str
    evidence: ProviderEvidence | None = None

    def __post_init__(self):
        _text(self.profile_id, "profile_id")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.profile_id) is None:
            raise ValueError("profile_id must be a safe lower-case identifier")
        _selection(self.provider_id, self.model_id)
        _text(self.model_identity, "model_identity")
        _text(self.endpoint, "endpoint")
        parsed = urlparse(self.endpoint)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("endpoint has an invalid port") from exc
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or port is None or not 1 <= port <= 65535
                or parsed.path != "/v1/chat/completions" or parsed.params
                or parsed.query or parsed.fragment):
            raise ValueError("endpoint must be a local chat-completions URL with an explicit port")
        if type(self.context_length) is not int or self.context_length <= 0:
            raise ValueError("context_length must be a positive integer requirement")
        if type(self.parallel) is not int or self.parallel != 1:
            raise ValueError("profile must require one serial worker")
        if type(self.temperature) is not int or self.temperature != 0:
            raise ValueError("profile must retain the existing temperature-zero transport")
        if self.evidence is not None:
            if type(self.evidence) is not ProviderEvidence:
                raise ValueError("evidence must be a ProviderEvidence record")
            if self.evidence.model_identity != self.model_identity:
                raise ValueError("evidence must identify the profile model")
            if self.evidence.profile_fingerprint != self.fingerprint:
                raise ValueError("evidence must bind the complete provider profile")

    @property
    def fingerprint(self) -> str:
        """Bind evidence to all transport and required runtime settings."""
        fields = {name: getattr(self, name) for name in (
            "profile_id", "provider_id", "model_id", "endpoint", "context_length",
            "parallel", "temperature", "model_identity",
        )}
        raw = json.dumps(fields, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @property
    def verification_status(self) -> str:
        return "not_tested" if self.evidence is None else self.evidence.status


class ModelManager:
    """Immutable registry; deliberately has no active/default model or loader."""

    def __init__(self, profiles: tuple[ProviderProfile, ...]):
        if (type(profiles) is not tuple or not profiles
                or any(type(profile) is not ProviderProfile for profile in profiles)):
            raise ValueError("profiles must be a non-empty tuple of ProviderProfile records")
        ids = set()
        registry = {}
        for profile in profiles:
            key = (profile.provider_id, profile.model_id)
            if profile.profile_id in ids or key in registry:
                raise ValueError("duplicate profile ID or ambiguous provider/model registration")
            ids.add(profile.profile_id)
            registry[key] = profile
        self._registry = MappingProxyType(registry)

    def list_profiles(self) -> tuple[ProviderProfile, ...]:
        return tuple(self._registry.values())

    def list_models(self, provider_id: str) -> tuple[str, ...]:
        if type(provider_id) is not str or provider_id not in _PROVIDERS:
            raise ValueError("unknown local provider")
        return tuple(model for provider, model in self._registry if provider == provider_id)

    def select_model(self, selection: ModelSelection) -> ProviderProfile:
        if type(selection) is not ModelSelection:
            raise ValueError("an explicit ModelSelection is required")
        try:
            return self._registry[(selection.provider_id, selection.model_id)]
        except KeyError as exc:
            raise ValueError("model is not registered for the explicitly selected provider") from exc

    def create_chat_client(
        self, selection: ModelSelection, *, response_kind: str = "tool_action",
        timeout_seconds: float = 120.0, opener=urlopen,
        execution_copy_field_order: bool = False,
    ) -> LMStudioChatClient:
        profile = self.select_model(selection)
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 300):
            raise ValueError("timeout_seconds must be finite and within the 300-second call budget")
        return LMStudioChatClient(
            model=profile.model_id, endpoint=profile.endpoint,
            response_kind=response_kind, timeout_seconds=timeout_seconds, opener=opener,
            execution_copy_field_order=execution_copy_field_order,
        )

    def bind_chat_factory(self, selection: ModelSelection, **client_options):
        """Bind a Supervisor factory to the explicit provider AND model choice."""
        profile = self.select_model(selection)
        # Validate options before returning a factory. Construction does no I/O.
        self.create_chat_client(selection, **client_options)

        def factory(model_id: str) -> LMStudioChatClient:
            if type(model_id) is not str or model_id != profile.model_id:
                raise ValueError("task model differs from the explicitly selected model")
            return self.create_chat_client(selection, **client_options)

        return factory


@dataclass(frozen=True)
class ModelRecommendation:
    selection: ModelSelection
    reason: str
    verification_status: str


class ModelAdvisor:
    """List candidates and declared evidence; never rank untested build quality."""

    def __init__(self, manager: ModelManager):
        if type(manager) is not ModelManager:
            raise ValueError("advisor requires a ModelManager")
        self._manager = manager

    def recommend(self, provider_id: str | None = None) -> tuple[ModelRecommendation, ...]:
        if provider_id is not None:
            self._manager.list_models(provider_id)
        return tuple(
            ModelRecommendation(
                ModelSelection(profile.provider_id, profile.model_id),
                "Registered local schema-transport candidate; "
                f"declared evidence: {profile.verification_status}. "
                "User selection and live runtime checks are required before execution.",
                profile.verification_status,
            )
            for profile in self._manager.list_profiles()
            if provider_id is None or profile.provider_id == provider_id
        )
