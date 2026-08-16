"""RND-356 (T2) — LLM provider abstraction tests."""

from __future__ import annotations

import httpx
import pytest

from app.services.ai.llm_provider import (
    DeepSeekProvider,
    FakeProvider,
    LLMMessage,
    LLMProviderConfigurationError,
    LLMProviderDisabledError,
    LLMProviderProtocolError,
    NullProvider,
    ai_support_is_enabled,
    get_llm_provider,
)
from app.settings import AiSettings


def test_null_provider_always_refuses() -> None:
    provider = NullProvider()
    with pytest.raises(LLMProviderDisabledError):
        provider.generate([LLMMessage(role="user", content="hi")])


def test_fake_provider_returns_fixed_text() -> None:
    provider = FakeProvider("固定回答")
    response = provider.generate([LLMMessage(role="user", content="问题")])
    assert response.text == "固定回答"
    assert response.provider == "fake"


def test_fake_provider_delegates_to_callable() -> None:
    provider = FakeProvider(lambda messages: f"echo:{messages[-1].content}")
    response = provider.generate([LLMMessage(role="user", content="hello")])
    assert response.text == "echo:hello"


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("on", True), ("false", False), ("", False), ("0", False), ("garbage", False)])
def test_ai_support_is_enabled_parsing(raw: str, expected: bool) -> None:
    settings = AiSettings(ai_support_enabled=raw)
    assert ai_support_is_enabled(settings) is expected


def test_get_llm_provider_returns_null_when_disabled() -> None:
    settings = AiSettings(ai_support_enabled="false")
    provider = get_llm_provider(settings)
    assert isinstance(provider, NullProvider)


def test_get_llm_provider_requires_provider_name_when_enabled() -> None:
    settings = AiSettings(ai_support_enabled="true", ai_llm_provider="")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm_provider(settings)


def test_get_llm_provider_rejects_unknown_provider() -> None:
    settings = AiSettings(ai_support_enabled="true", ai_llm_provider="openai")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm_provider(settings)


def test_get_llm_provider_deepseek_requires_api_key() -> None:
    settings = AiSettings(ai_support_enabled="true", ai_llm_provider="deepseek", ai_llm_model="deepseek-chat")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm_provider(settings)


def test_get_llm_provider_deepseek_requires_model() -> None:
    settings = AiSettings(
        ai_support_enabled="true", ai_llm_provider="deepseek", deepseek_api_key="sk-test", ai_llm_model=""
    )
    with pytest.raises(LLMProviderConfigurationError):
        get_llm_provider(settings)


def test_get_llm_provider_deepseek_succeeds_when_configured() -> None:
    settings = AiSettings(
        ai_support_enabled="true", ai_llm_provider="deepseek", deepseek_api_key="sk-test", ai_llm_model="deepseek-chat"
    )
    provider = get_llm_provider(settings)
    assert isinstance(provider, DeepSeekProvider)


def test_deepseek_provider_success(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeepSeekProvider(api_key="sk-test", model="deepseek-chat", base_url="https://api.deepseek.com")

    def fake_post(self, url, headers=None, json=None):
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "回答内容"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    response = provider.generate([LLMMessage(role="user", content="问题")])
    assert response.text == "回答内容"
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 4
    assert response.model == "deepseek-chat"


def test_deepseek_provider_non_200_raises_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeepSeekProvider(api_key="sk-test", model="deepseek-chat", base_url="https://api.deepseek.com")

    def fake_post(self, url, headers=None, json=None):
        return httpx.Response(500, text="internal error", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMProviderProtocolError):
        provider.generate([LLMMessage(role="user", content="问题")])


def test_deepseek_provider_malformed_response_raises_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeepSeekProvider(api_key="sk-test", model="deepseek-chat", base_url="https://api.deepseek.com")

    def fake_post(self, url, headers=None, json=None):
        return httpx.Response(200, json={"unexpected": "shape"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMProviderProtocolError):
        provider.generate([LLMMessage(role="user", content="问题")])


def test_deepseek_provider_network_error_raises_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DeepSeekProvider(api_key="sk-test", model="deepseek-chat", base_url="https://api.deepseek.com")

    def fake_post(self, url, headers=None, json=None):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    with pytest.raises(LLMProviderProtocolError):
        provider.generate([LLMMessage(role="user", content="问题")])
