"""Provider-neutral LLM boundary (RND-356 / T2), mirroring the shape of
app/services/payment_provider.py: a small Protocol the answer service codes
against, with swappable implementations behind it. Hosted deployments
configure a real provider; self-hosted deployments can leave it unset and
get a safe, explicit "disabled" refusal instead of a crash or a silent call
to a nonexistent model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import httpx

from app.settings import AiSettings, get_ai_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage
    provider: str
    model: str


class LLMProviderError(Exception):
    """Base class for all provider failures the answer service must treat
    as "cannot answer right now", never as license to fabricate a response."""


class LLMProviderDisabledError(LLMProviderError):
    """Raised by NullProvider — AI support is not configured/enabled."""


class LLMProviderConfigurationError(LLMProviderError):
    """A real provider was selected but is missing required configuration
    (e.g. DEEPSEEK_API_KEY unset). Never call the provider with a guessed
    or empty credential."""


class LLMProviderProtocolError(LLMProviderError):
    """The provider responded, but not in the shape this client understands
    (bad status code, missing fields, malformed JSON)."""


class LLMProvider(Protocol):
    name: str

    def generate(self, messages: list[LLMMessage], *, max_tokens: int = 800) -> LLMResponse:
        ...


class NullProvider:
    """Self-hosted-disabled / unconfigured default. Every call refuses
    loudly and immediately — never silently returns a canned answer that
    could be mistaken for a real one."""

    name = "null"

    def generate(self, messages: list[LLMMessage], *, max_tokens: int = 800) -> LLMResponse:
        raise LLMProviderDisabledError("AI support is not enabled or no LLM provider is configured")


class FakeProvider:
    """Deterministic provider for tests (RND-356 AC: "provider fake 实现下的
    确定性集成测试"). Either returns a fixed string or delegates to a
    caller-supplied function of the message list, so tests can assert on
    exactly what the answer service sent as context."""

    name = "fake"

    def __init__(self, respond: Callable[[list[LLMMessage]], str] | str = ""):
        self._respond = respond

    def generate(self, messages: list[LLMMessage], *, max_tokens: int = 800) -> LLMResponse:
        text = self._respond(messages) if callable(self._respond) else self._respond
        return LLMResponse(
            text=text,
            usage=LLMUsage(prompt_tokens=sum(len(m.content) for m in messages) // 4, completion_tokens=len(text) // 4),
            provider=self.name,
            model="fake-deterministic",
        )


class DeepSeekProvider:
    """OpenAI-compatible chat-completions client for DeepSeek. Only httpx is
    used — no new dependency — because DeepSeek's REST API follows the
    OpenAI chat/completions request/response shape.

    The exact model identifier is never guessed here: AI_LLM_MODEL must be
    set explicitly (e.g. to whatever DeepSeek publishes as its current
    "flash"-tier model id at deploy time). An empty value is a configuration
    error, not a fallback to some hardcoded string that might silently stop
    matching DeepSeek's real catalog.
    """

    name = "deepseek"

    def __init__(self, api_key: str, model: str, base_url: str, *, timeout_seconds: float = 30.0):
        if not api_key:
            raise LLMProviderConfigurationError("DEEPSEEK_API_KEY is not set")
        if not model:
            raise LLMProviderConfigurationError("AI_LLM_MODEL is not set")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def generate(self, messages: list[LLMMessage], *, max_tokens: int = 800) -> LLMResponse:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        try:
            # trust_env=False: never silently route API calls through an
            # ambient HTTP(S)_PROXY/ALL_PROXY the host happens to have set —
            # this provider's network behavior must be fully determined by
            # its own explicit configuration.
            with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            logger.error("DeepSeek request failed: %s", type(exc).__name__)
            raise LLMProviderProtocolError("DeepSeek request failed") from exc

        if resp.status_code != 200:
            logger.error("DeepSeek returned unexpected status %s", resp.status_code)
            raise LLMProviderProtocolError(f"DeepSeek returned HTTP {resp.status_code}")

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            logger.error("DeepSeek response shape unexpected: %s", type(exc).__name__)
            raise LLMProviderProtocolError("DeepSeek response missing expected fields") from exc

        return LLMResponse(
            text=text,
            usage=LLMUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            ),
            provider=self.name,
            model=self._model,
        )


def ai_support_is_enabled(settings: Optional[AiSettings] = None) -> bool:
    raw = (settings or get_ai_settings()).ai_support_enabled.strip().lower()
    return raw in {"1", "true", "yes", "on"}


def ai_public_support_is_enabled(settings: Optional[AiSettings] = None) -> bool:
    """RND-408: public AI support is enabled only when both the overall
    AI kill switch AND the public-specific switch are on."""
    source = settings or get_ai_settings()
    if not ai_support_is_enabled(source):
        return False
    raw = source.ai_public_support_enabled.strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_llm_provider(settings: Optional[AiSettings] = None) -> LLMProvider:
    """Single factory every caller (answer_service, eval scripts) must use
    instead of constructing a provider directly — this is what makes
    AI_SUPPORT_ENABLED a real kill switch rather than a UI-only hint."""
    source = settings or get_ai_settings()
    if not ai_support_is_enabled(source):
        return NullProvider()

    provider_name = source.ai_llm_provider.strip().lower()
    if provider_name == "deepseek":
        return DeepSeekProvider(
            api_key=source.deepseek_api_key.strip(),
            model=source.ai_llm_model.strip(),
            base_url=source.deepseek_base_url.strip() or "https://api.deepseek.com",
        )
    if provider_name == "fake":
        return FakeProvider()
    if not provider_name:
        raise LLMProviderConfigurationError("AI_SUPPORT_ENABLED is true but AI_LLM_PROVIDER is not set")
    raise LLMProviderConfigurationError(f"Unknown AI_LLM_PROVIDER: {provider_name!r}")
