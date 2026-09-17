"""Tests for AnthropicProvider streaming delta forwarding."""

import asyncio
import types
import unittest
from typing import List, Optional
from unittest.mock import MagicMock

from kerkov.models.basic_models import Message
from kerkov.providers.anthrophic_provider import AnthropicProvider


def _text_block(text: str):
    return types.SimpleNamespace(type="text", text=text)


def _final_message(text: str):
    return types.SimpleNamespace(
        content=[_text_block(text)],
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=3),
    )


class _FakeStream:
    """Mimics the anthropic streaming context manager surface we use."""

    def __init__(self, chunks: List[str]) -> None:
        self._chunks = chunks
        self.text_stream = self._aiter()
        self.iterated = False

    async def _aiter(self):
        self.iterated = True
        for chunk in self._chunks:
            yield chunk

    async def get_final_message(self):
        return _final_message("".join(self._chunks))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _provider_with_stream(stream: _FakeStream) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="test-key", model="claude-test")
    provider.client = MagicMock()
    provider.client.messages.stream = MagicMock(return_value=stream)
    return provider


class AnthropicStreamingTest(unittest.TestCase):
    def test_deltas_forwarded_and_final_assembled(self):
        stream = _FakeStream(["Hel", "lo ", "world"])
        provider = _provider_with_stream(stream)
        deltas: List[str] = []

        response = asyncio.run(
            provider.complete(
                [Message(role="user", content="hi")],
                on_delta=deltas.append,
            )
        )
        self.assertEqual(deltas, ["Hel", "lo ", "world"])
        self.assertEqual(response.content, "Hello world")
        self.assertEqual(response.usage.completion_tokens, 3)

    def test_no_callback_skips_text_stream_iteration(self):
        stream = _FakeStream(["ignored"])
        provider = _provider_with_stream(stream)

        response = asyncio.run(provider.complete([Message(role="user", content="hi")]))
        self.assertFalse(stream.iterated)
        self.assertEqual(response.content, "ignored")


if __name__ == "__main__":
    unittest.main()
