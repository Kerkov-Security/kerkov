"""Tests for the NVIDIA NIM provider's OpenAI-compatible message handling."""

import json

from kerkov.models.basic_models import Message
from kerkov.providers.nvidia_provider import NvidiaProvider


def _provider() -> NvidiaProvider:
    return NvidiaProvider(
        api_key="test-key",
        base_url="https://integrate.api.nvidia.com/v1",
        model="z-ai/glm-5.2",
    )


def test_tool_calls_are_parsed() -> None:
    provider = _provider()
    calls = provider._parse_tool_calls(
        [
            {
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                },
            }
        ]
    )

    assert len(calls) == 1
    assert calls[0].tool_name == "read_file"
    assert calls[0].parameters == {"path": "README.md"}
    assert calls[0].call_id == "call_123"


def test_tool_call_and_result_round_trip() -> None:
    provider = _provider()
    raw_tool_calls = [
        {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
        }
    ]
    messages = [
        Message(role="user", content="Read the README"),
        Message(
            role="assistant",
            content="",
            raw_blocks=[
                {"role": "assistant", "content": None, "tool_calls": raw_tool_calls}
            ],
        ),
        Message(role="tool", content="contents", call_id="call_123"),
    ]

    converted = provider._convert_messages(messages)

    assert converted[1]["tool_calls"] == raw_tool_calls
    assert converted[2] == {
        "role": "tool",
        "content": "contents",
        "tool_call_id": "call_123",
    }


def test_plain_messages_are_openai_compatible() -> None:
    converted = _provider()._convert_messages(
        [
            Message(role="system", content="Be concise"),
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi"),
        ]
    )

    assert converted == [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]
