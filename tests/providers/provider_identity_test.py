"""Provider identity stays canonical across SDK classes and older metadata."""

from unittest.mock import Mock

import pytest

from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.providers.base_provider import normalize_provider_name, provider_name
from kerkov.providers.codex.provider import CodexProvider
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.providers.gemini_provider import GeminiProvider
from kerkov.providers.nvidia_provider import NvidiaProvider
from kerkov.providers.ollama_provider import OllamaProvider
from kerkov.providers.openrouter_provider import OpenRouterProvider


@pytest.mark.parametrize(
    "provider_class,canonical",
    [
        (AnthropicProvider, "anthropic"),
        (GeminiProvider, "gemini"),
        (NvidiaProvider, "nvidia"),
        (OpenRouterProvider, "openrouter"),
        (DeepSeekProvider, "deepseek"),
        (OllamaProvider, "ollama"),
        (CodexProvider, "codex"),
    ],
)
def test_builtin_model_info_exposes_canonical_identity(provider_class, canonical):
    provider = object.__new__(provider_class)
    provider.model = "selected-model"

    assert provider.get_model_info() == {
        "provider": canonical,
        "model": "selected-model",
    }
    assert provider_name(provider) == canonical
    assert normalize_provider_name(provider_class.__name__) == canonical


@pytest.mark.parametrize("legacy", ["AnthropicProvider", "ANTHROPIC", "claude"])
def test_declared_legacy_identity_is_canonicalized(legacy):
    provider = Mock()
    provider.get_model_info.return_value = {"provider": legacy}

    assert provider_name(provider) == "anthropic"


def test_custom_identity_is_preserved():
    provider = Mock()
    provider.get_model_info.return_value = {"provider": "CustomProvider"}

    assert provider_name(provider) == "CustomProvider"
    assert normalize_provider_name("CustomProvider") == "CustomProvider"


@pytest.mark.parametrize("info", [None, {}, {"provider": Mock()}, {"provider": " "}])
def test_invalid_identity_falls_back_to_class_name(info):
    provider = object.__new__(CodexProvider)
    provider.get_model_info = Mock(return_value=info)

    assert provider_name(provider) == "codex"
