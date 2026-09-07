"""Tests for the OpenAI-compatible SSE stream reassembler."""

import asyncio
import unittest
from typing import AsyncIterator, List

from kerkov.providers.openai_stream import reconstruct_openai_stream


async def _aiter(lines: List[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


def _run(lines, on_delta=None):
    return asyncio.run(reconstruct_openai_stream(_aiter(lines), on_delta))


class ReconstructOpenAIStreamTest(unittest.TestCase):
    def test_content_deltas_concatenate_and_forward(self):
        deltas: List[str] = []
        data = _run(
            [
                'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                'data: {"choices":[{"delta":{"content":"Hel"}}]}',
                'data: {"choices":[{"delta":{"content":"lo"}}]}',
                "data: [DONE]",
            ],
            deltas.append,
        )
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello")

    def test_tool_call_fragments_are_reassembled(self):
        data = _run(
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                '"type":"function","function":{"name":"exec","arguments":"{\\"cmd\\""}}]}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"arguments":":\\"ls\\"}"}}]}}]}',
                'data: {"choices":[{"finish_reason":"tool_calls","delta":{}}]}',
                "data: [DONE]",
            ]
        )
        tool_calls = data["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["id"], "call_1")
        self.assertEqual(tool_calls[0]["function"]["name"], "exec")
        self.assertEqual(tool_calls[0]["function"]["arguments"], '{"cmd":"ls"}')
        self.assertEqual(data["choices"][0]["finish_reason"], "tool_calls")

    def test_usage_and_model_are_captured(self):
        data = _run(
            [
                'data: {"model":"m1","choices":[{"delta":{"content":"x"}}]}',
                'data: {"usage":{"prompt_tokens":10,"completion_tokens":5,'
                '"total_tokens":15},"choices":[]}',
                "data: [DONE]",
            ]
        )
        self.assertEqual(data["model"], "m1")
        self.assertEqual(data["usage"]["prompt_tokens"], 10)
        self.assertEqual(data["usage"]["total_tokens"], 15)

    def test_malformed_and_non_data_lines_are_skipped(self):
        data = _run(
            [
                ": comment / keep-alive",
                "",
                "data: not-json",
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                "data: [DONE]",
            ]
        )
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")

    def test_no_tool_calls_leaves_message_without_the_key(self):
        data = _run(['data: {"choices":[{"delta":{"content":"hi"}}]}', "data: [DONE]"])
        self.assertNotIn("tool_calls", data["choices"][0]["message"])


if __name__ == "__main__":
    unittest.main()
