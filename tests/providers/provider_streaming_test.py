"""Streaming wiring for NVIDIA, Ollama, and Gemini providers."""

import asyncio
import json
import types
import unittest
from typing import List
from unittest.mock import MagicMock, patch

from kerkov.models.basic_models import Message


class _FakeResponse:
    def __init__(self, status_code: int, lines: List[str]) -> None:
        self.status_code = status_code
        self._lines = lines
        self.text = ""

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeStreamCM:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, lines: List[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None):
        return _FakeStreamCM(_FakeResponse(200, self._lines))


class NvidiaStreamingTest(unittest.TestCase):
    def test_streaming_forwards_deltas_and_parses_tool_calls(self):
        from kerkov.providers.nvidia_provider import NvidiaProvider

        lines = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"model":"m","usage":{"prompt_tokens":7,"completion_tokens":2,'
            '"total_tokens":9},"choices":[{"finish_reason":"stop","delta":{}}]}',
            "data: [DONE]",
        ]
        provider = NvidiaProvider(
            api_key="k", base_url="https://x/v1", model="z-ai/glm-5.2"
        )
        deltas: List[str] = []
        with patch(
            "kerkov.providers.nvidia_provider.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(lines),
        ):
            response = asyncio.run(
                provider.complete(
                    [Message(role="user", content="hi")],
                    on_delta=deltas.append,
                )
            )
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(response.content, "Hello")
        self.assertEqual(response.usage.prompt_tokens, 7)


class OllamaStreamingTest(unittest.TestCase):
    def test_ndjson_stream_folds_content_and_usage(self):
        from kerkov.providers.ollama_provider import OllamaProvider

        lines = [
            json.dumps({"message": {"content": "Hel"}, "done": False}),
            json.dumps({"message": {"content": "lo"}, "done": False}),
            json.dumps(
                {
                    "message": {"content": ""},
                    "done": True,
                    "prompt_eval_count": 12,
                    "eval_count": 4,
                }
            ),
        ]
        provider = OllamaProvider(base_url="http://x", model="gpt-oss")
        deltas: List[str] = []
        with patch(
            "kerkov.providers.ollama_provider.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(lines),
        ):
            response = asyncio.run(
                provider.complete(
                    [Message(role="user", content="hi")],
                    on_delta=deltas.append,
                )
            )
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(response.content, "Hello")
        self.assertEqual(response.usage.prompt_tokens, 12)
        self.assertEqual(response.usage.completion_tokens, 4)

    def test_mid_stream_error_raises(self):
        from kerkov.providers.ollama_provider import OllamaProvider

        lines = [
            json.dumps({"message": {"content": "The scan shows "}, "done": False}),
            json.dumps({"error": "an error was encountered while running the model"}),
        ]
        provider = OllamaProvider(base_url="http://x", model="gpt-oss")
        with patch(
            "kerkov.providers.ollama_provider.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(lines),
        ):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(
                    provider.complete(
                        [Message(role="user", content="hi")],
                        on_delta=lambda _t: None,
                    )
                )
        self.assertIn("error was encountered", str(ctx.exception))


class OpenRouterStreamingTest(unittest.TestCase):
    def test_mid_stream_error_raises(self):
        from kerkov.providers.openrouter_provider import OpenRouterProvider

        lines = [
            'data: {"choices":[{"delta":{"content":"The scan shows "}}]}',
            'data: {"error":{"code":500,"message":"mid-stream failure"}}',
            "data: [DONE]",
        ]
        provider = OpenRouterProvider(
            api_key="k", base_url="https://x/api/v1", model="openrouter/auto"
        )
        with patch(
            "kerkov.providers.openrouter_provider.httpx.AsyncClient",
            lambda **kw: _FakeAsyncClient(lines),
        ):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(
                    provider.complete(
                        [Message(role="user", content="hi")],
                        on_delta=lambda _t: None,
                    )
                )
        self.assertIn("mid-stream failure", str(ctx.exception))


def _gemini_chunk(text: str, *, usage=None):
    part = types.SimpleNamespace(text=text, function_call=None)
    content = types.SimpleNamespace(parts=[part])
    candidate = types.SimpleNamespace(content=content)
    chunk = types.SimpleNamespace(candidates=[candidate])
    if usage is not None:
        chunk.usage_metadata = usage
    return chunk


class GeminiStreamingTest(unittest.TestCase):
    def test_streaming_forwards_text_and_captures_usage(self):
        import kerkov.providers.gemini_provider as gp

        usage = types.SimpleNamespace(
            prompt_token_count=8,
            candidates_token_count=3,
            total_token_count=11,
        )
        chunks = [
            _gemini_chunk("Hel"),
            _gemini_chunk("lo", usage=usage),
        ]

        async def _agen():
            for c in chunks:
                yield c

        model = MagicMock()

        async def _generate(messages, generation_config=None, stream=False):
            self.assertTrue(stream)
            return _agen()

        model.generate_content_async = _generate

        provider = gp.GeminiProvider(api_key="k", model="gemini-2.5-flash")
        deltas: List[str] = []
        with patch.object(gp.genai, "GenerativeModel", return_value=model):
            response = asyncio.run(
                provider.complete(
                    [Message(role="user", content="hi")],
                    on_delta=deltas.append,
                )
            )
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(response.content, "Hello")
        self.assertEqual(response.usage.total_tokens, 11)


if __name__ == "__main__":
    unittest.main()
