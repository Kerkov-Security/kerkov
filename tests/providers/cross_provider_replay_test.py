"""Portable final answers and native provider history survive replay/resume."""

from copy import deepcopy

import pytest
from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.conversation_engine import extract_raw_blocks
from kerkov.models.basic_models import Message
from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.providers.codex.provider import CodexProvider

CHAT_FINALS = [
    pytest.param(
        [{"role": "assistant", "content": "hello", "reasoning_content": "thought"}],
        id="deepseek",
    ),
    pytest.param([{"role": "assistant", "content": "hello"}], id="openrouter"),
    pytest.param(
        [{"role": "assistant", "content": "hello", "tool_calls": None}], id="nvidia"
    ),
]
PORTABLE_FINALS = CHAT_FINALS + [
    pytest.param([], id="empty-output"),
    pytest.param([{"type": "unknown_output", "text": "hello"}], id="unknown-output"),
]
CODEX_FINAL = [
    {
        "id": "rs_final",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-reasoning-payload",
    },
    {
        "id": "msg_final",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    },
]


def _history(raw_blocks):
    return [
        Message(role="user", content="greet me"),
        Message(
            role="assistant",
            content="hello",
            raw_blocks=extract_raw_blocks({"output": deepcopy(raw_blocks)}),
        ),
    ]


def _resume(tmp_path, history):
    store = EngagementStore(store_dir=tmp_path)
    engagement_id = store.start_engagement("audit", "test", "standard", "normal")
    for message in history:
        store.append_message(message)
    resumed = EngagementStore(store_dir=tmp_path).load_engagement(engagement_id)
    assert resumed is not None
    return resumed.messages


def _codex():
    return CodexProvider(
        model="test", base_url="https://example.invalid", access_token="test-token"
    )


@pytest.mark.parametrize("persisted", [False, True], ids=["live", "resumed"])
@pytest.mark.parametrize(
    "raw_blocks", PORTABLE_FINALS + [pytest.param(CODEX_FINAL, id="codex")]
)
def test_anthropic_replays_foreign_final_answer_as_text(
    tmp_path, raw_blocks, persisted
):
    history = _history(raw_blocks)
    if persisted:
        history = _resume(tmp_path, history)
    history.append(Message(role="user", content="continue"))
    provider = AnthropicProvider(api_key="test-key", model="test")

    request = provider._build_request(history, None, None, None)

    assert request["messages"][1] == {"role": "assistant", "content": "hello"}
    assert len(request["messages"]) == 3


@pytest.mark.parametrize("persisted", [False, True], ids=["live", "resumed"])
@pytest.mark.parametrize(
    "raw_blocks",
    PORTABLE_FINALS
    + [
        pytest.param([TextBlock(type="text", text="hello")], id="anthropic-sdk"),
        pytest.param([{"type": "text", "text": "hello"}], id="gemini"),
    ],
)
def test_codex_replays_foreign_final_answer_as_text(tmp_path, raw_blocks, persisted):
    history = _history(raw_blocks)
    if persisted:
        history = _resume(tmp_path, history)
    history.append(Message(role="user", content="continue"))

    items = _codex()._convert_messages_to_responses_input(history)

    assert items[1] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello"}],
    }
    assert len(items) == 3


@pytest.mark.parametrize("persisted", [False, True], ids=["live", "resumed"])
def test_anthropic_preserves_native_reasoning_and_tool_history(tmp_path, persisted):
    blocks = [
        ThinkingBlock(type="thinking", thinking="thought", signature="signature"),
        TextBlock(type="text", text="checking"),
        ToolUseBlock(type="tool_use", id="call_test", name="exec", input={}),
    ]
    history = [
        Message(role="assistant", content="checking", raw_blocks=blocks),
        Message(role="tool", content="result", call_id="call_test"),
    ]
    if persisted:
        history = _resume(tmp_path, history)
    provider = AnthropicProvider(api_key="test-key", model="test")

    converted = provider._convert_messages(history)

    assert converted[0]["content"] == history[0].raw_blocks
    assert converted[1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_test", "content": "result"}
        ],
    }


def test_anthropic_accepts_compatible_gemini_text_blocks():
    blocks = [{"type": "text", "text": "hello"}]
    provider = AnthropicProvider(api_key="test-key", model="test")

    converted = provider._convert_messages(_history(blocks))

    assert converted[1] == {"role": "assistant", "content": blocks}


@pytest.mark.parametrize("persisted", [False, True], ids=["live", "resumed"])
def test_codex_preserves_final_encrypted_reasoning_on_next_turn(tmp_path, persisted):
    history = _history(CODEX_FINAL)
    if persisted:
        history = _resume(tmp_path, history)
    history.append(Message(role="user", content="continue"))

    items = _codex()._convert_messages_to_responses_input(history)

    assert items[1:3] == CODEX_FINAL
    assert items[3] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "continue"}],
    }
    assert len(items) == 4


def test_codex_preserves_native_reasoning_and_tool_history():
    blocks = [
        CODEX_FINAL[0],
        {
            "id": "fc_test",
            "type": "function_call",
            "call_id": "call_test",
            "name": "exec",
            "arguments": "{}",
        },
    ]
    history = [
        Message(role="assistant", content="", raw_blocks=blocks),
        Message(role="tool", content="result", call_id="call_test"),
    ]

    items = _codex()._convert_messages_to_responses_input(history)

    assert items == blocks + [
        {"type": "function_call_output", "call_id": "call_test", "output": "result"}
    ]
