"""Every tool's parameters reach every provider in the normalized schema shape."""

import importlib
import json
import os
import pkgutil
from typing import Any, Callable, Dict, List, Optional, Type

import pytest

import kerkov.agents
import kerkov.tools
from kerkov.core.approval_scope import ApprovalScope
from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.providers.codex.provider import CodexProvider
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.providers.gemini_provider import GeminiProvider
from kerkov.providers.nvidia_provider import NvidiaProvider
from kerkov.providers.ollama_provider import OllamaProvider
from kerkov.providers.openrouter_provider import OpenRouterProvider
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult
from kerkov.tools.compliance_scan_tool import ComplianceScanParams

_VALIDATION_KEYWORDS = {
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "additionalProperties",
}


def _registered_params_classes() -> List[Type[ToolParams]]:
    for package in (kerkov.tools, kerkov.agents):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            importlib.import_module(module.name)

    found: Dict[str, Type[ToolParams]] = {}
    pending = list(ToolParams.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("kerkov."):
            found[f"{cls.__module__}.{cls.__qualname__}"] = cls
    return [found[name] for name in sorted(found)]


class _SchemaTool(BaseTool):
    def __init__(
        self, params_class: Type[ToolParams], name: str = "schema_probe"
    ) -> None:
        super().__init__(name, "schema probe")
        self._params_class = params_class

    def get_params_class(self) -> Type[ToolParams]:
        return self._params_class

    async def run(self, toolParams: ToolParams) -> ToolResult:
        return ToolResult(success=True, output="")

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        return None

    def approval_scope(self, params: Dict[str, Any]) -> ApprovalScope:
        return ApprovalScope.exact_only()


def _anthropic(tool: BaseTool) -> Dict[str, Any]:
    provider = AnthropicProvider(api_key="test-key", model="claude-test")
    return provider._convert_to_anthropic_tool_schema(tool)["input_schema"]


def _deepseek(tool: BaseTool) -> Dict[str, Any]:
    provider = DeepSeekProvider(
        api_key="k", base_url="https://api.deepseek.com", model="deepseek-flash"
    )
    return provider._convert_tool_schema(tool)["function"]["parameters"]


def _openrouter(tool: BaseTool) -> Dict[str, Any]:
    provider = OpenRouterProvider(
        api_key="k", base_url="https://openrouter.ai/api/v1", model="openai/gpt-4"
    )
    return provider._convert_tool_schema(tool)["function"]["parameters"]


def _gemini(tool: BaseTool) -> Dict[str, Any]:
    provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
    declaration = provider._convert_to_gemini_tool_schema(tool)
    assert declaration.parameters is None
    assert declaration.parameters_json_schema is not None
    return declaration.parameters_json_schema


def _nvidia(tool: BaseTool) -> Dict[str, Any]:
    provider = NvidiaProvider(
        api_key="k", base_url="https://integrate.api.nvidia.com/v1", model="m"
    )
    return provider._convert_tool_schema(tool)["function"]["parameters"]


def _ollama(tool: BaseTool) -> Dict[str, Any]:
    provider = OllamaProvider("http://localhost:11434", "m")
    return provider._convert_to_ollama_tool_schema(tool)["function"]["parameters"]


def _codex(tool: BaseTool) -> Dict[str, Any]:
    provider = CodexProvider(
        model="gpt-5.4", base_url="https://chatgpt.com/backend-api", access_token="t"
    )
    return provider._convert_tool(tool)["parameters"]


def test_codex_keeps_optional_scan_arguments_optional_at_the_responses_boundary():
    provider = CodexProvider(
        model="gpt-5.4", base_url="https://chatgpt.com/backend-api", access_token="t"
    )
    converted = provider._convert_tool(_SchemaTool(ComplianceScanParams))
    assert converted["strict"] is False
    assert "inputs_file" not in converted["parameters"]["required"]
    assert converted["parameters"]["properties"]["inputs_file"]["type"] == "string"


_CONVERTERS: Dict[str, Callable[[BaseTool], Dict[str, Any]]] = {
    "anthropic": _anthropic,
    "deepseek": _deepseek,
    "gemini": _gemini,
    "openrouter": _openrouter,
    "nvidia": _nvidia,
    "ollama": _ollama,
    "codex": _codex,
}


def _schema_keywords(node: Any) -> set:
    """Keywords used in schema position, skipping names under `properties`."""
    found: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key == "properties" and isinstance(value, dict):
                for sub in value.values():
                    found |= _schema_keywords(sub)
            elif key not in {"enum", "const", "default", "required", "examples"}:
                found |= _schema_keywords(value)
    elif isinstance(node, list):
        for value in node:
            found |= _schema_keywords(value)
    return found


@pytest.mark.parametrize("provider", sorted(_CONVERTERS))
@pytest.mark.parametrize(
    "params_class", _registered_params_classes(), ids=lambda cls: cls.__name__
)
def test_tool_schema_is_normalized_for_provider(
    provider: str, params_class: Type[ToolParams]
) -> None:
    convert = _CONVERTERS[provider]
    tool = _SchemaTool(params_class)

    parameters = convert(tool)

    keywords = _schema_keywords(parameters)
    assert {"$ref", "$defs", "title", "definitions"}.isdisjoint(keywords)
    assert parameters["type"] == "object"
    expected = _schema_keywords(params_class.model_json_schema()) & _VALIDATION_KEYWORDS
    if provider == "gemini" and "const" in expected:
        expected = (expected - {"const"}) | {"enum"}
        assert "const" not in keywords
    assert expected <= keywords
    assert json.dumps(convert(tool)) == json.dumps(parameters)


def test_every_registered_params_class_is_covered() -> None:
    names = {cls.__name__ for cls in _registered_params_classes()}

    assert {"LedgerToolParams", "HTTPToolParams", "SastScanParams"} <= names


@pytest.mark.skipif(
    not (
        os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_SCHEMA_TEST_MODEL")
    ),
    reason="set GEMINI_API_KEY and GEMINI_SCHEMA_TEST_MODEL to validate schemas "
    "against the real Gemini API",
)
def test_gemini_api_accepts_every_tool_schema() -> None:
    from google import genai
    from google.genai import types

    api_key = os.environ["GEMINI_API_KEY"]
    model = os.environ["GEMINI_SCHEMA_TEST_MODEL"]
    provider = GeminiProvider(api_key=api_key, model=model)
    declarations = [
        provider._convert_to_gemini_tool_schema(_SchemaTool(cls, cls.__name__))
        for cls in _registered_params_classes()
    ]

    genai.Client(api_key=api_key).models.generate_content(
        model=model,
        contents="Reply with OK.",
        config=types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=declarations)],
            max_output_tokens=16,
        ),
    )
