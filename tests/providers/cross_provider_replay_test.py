"""Portable final answers and native provider history survive replay/resume."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import pytest
from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from kerkov.core.conversation_engine import extract_raw_blocks
from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.models.basic_models import Message
from kerkov.providers.anthrophic_provider import AnthropicProvider
from kerkov.providers.codex.provider import CodexProvider
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.providers.gemini_provider import GeminiProvider
from kerkov.providers.nvidia_provider import NvidiaProvider
from kerkov.providers.ollama_provider import OllamaProvider
from kerkov.providers.openrouter_provider import OpenRouterProvider

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


# Two calls catch conversions that only preserve the first raw block. Nested
# arguments exercise the object/JSON conversions used by the different APIs.
CALLS = [
    ("call_read", "read_file", {"path": "README.md", "options": {"lines": [1, 2]}}),
    ("call_exec", "exec", {"command": "pwd", "capture": True}),
]


def _tool_history(source):
    if source.startswith("anthropic"):
        blocks = [
            TextBlock(type="text", text="checking"),
            *[
                ToolUseBlock(type="tool_use", id=call_id, name=name, input=args)
                for call_id, name, args in CALLS
            ],
        ]
        if source == "anthropic-dict":
            blocks = [block.model_dump() for block in blocks]
    elif source == "codex":
        blocks = [
            CODEX_FINAL[0],
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "checking"}],
            },
            *[
                {
                    "type": "function_call",
                    "id": f"fc_{call_id}",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(args),
                }
                for call_id, name, args in CALLS
            ],
        ]
    elif source == "gemini":
        blocks = [
            {"type": "text", "text": "checking"},
            *[
                {"type": "function_call", "id": call_id, "name": name, "args": args}
                for call_id, name, args in CALLS
            ],
        ]
    else:
        blocks = [
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": (
                                args if source == "ollama" else json.dumps(args)
                            ),
                        },
                    }
                    for call_id, name, args in CALLS
                ],
            }
        ]
        if source == "deepseek":
            blocks[0]["reasoning_content"] = "native thought"
    return [
        Message(role="user", content="inspect"),
        Message(role="assistant", content="checking", raw_blocks=blocks),
        *[
            Message(role="tool", content=f"result {name}", call_id=call_id)
            for call_id, name, _ in CALLS
        ],
        Message(role="user", content="continue"),
    ]


def _converted_tool_exchange(destination, history):
    """Read the actual destination request into comparable call/result tuples."""
    calls, results = [], []
    if destination == "anthropic":
        provider = AnthropicProvider(api_key="test", model="test")
        for message in provider._convert_messages(history):
            if not isinstance(message["content"], list):
                continue
            for block in message["content"]:
                if hasattr(block, "model_dump"):
                    block = block.model_dump()
                if block["type"] == "tool_use":
                    calls.append((block["id"], block["name"], block["input"]))
                elif block["type"] == "tool_result":
                    results.append((block["tool_use_id"], block["content"]))
    elif destination == "codex":
        for item in _codex()._convert_messages_to_responses_input(history):
            if item["type"] == "function_call":
                calls.append(
                    (item["call_id"], item["name"], json.loads(item["arguments"]))
                )
            elif item["type"] == "function_call_output":
                results.append((item["call_id"], item["output"]))
    elif destination == "gemini":
        provider = GeminiProvider(api_key="test", model="test")
        for message in provider._convert_messages(history):
            for part in message.model_dump()["parts"]:
                if part.get("function_call"):
                    call = part["function_call"]
                    calls.append((call["name"], call["name"], call["args"]))
                if part.get("function_response"):
                    result = part["function_response"]
                    results.append((result["name"], result["response"]["result"]))
    else:
        if destination == "ollama":
            provider = OllamaProvider(base_url="https://example.invalid", model="test")
            messages = provider._convert_messages_to_chat(history)
        else:
            cls = {
                "deepseek": DeepSeekProvider,
                "openrouter": OpenRouterProvider,
                "nvidia": NvidiaProvider,
            }[destination]
            provider = cls(
                api_key="test", base_url="https://example.invalid", model="test"
            )
            messages = provider._convert_messages(history)
        for message in messages:
            for call in message.get("tool_calls", []):
                function = call["function"]
                args = function["arguments"]
                calls.append(
                    (
                        call.get("id", function["name"]),
                        function["name"],
                        json.loads(args) if isinstance(args, str) else args,
                    )
                )
            if message["role"] == "tool":
                results.append(
                    (
                        message.get("tool_call_id", message.get("tool_name")),
                        message["content"],
                    )
                )
    return calls, results


@pytest.mark.parametrize("persisted", [False, True], ids=["live", "resumed"])
@pytest.mark.parametrize(
    "source",
    [
        "anthropic-sdk",
        "anthropic-dict",
        "codex",
        "gemini",
        "deepseek",
        "openrouter",
        "nvidia",
        "ollama",
    ],
)
@pytest.mark.parametrize(
    "destination",
    [
        "anthropic",
        "codex",
        "gemini",
        "deepseek",
        "openrouter",
        "nvidia",
        "ollama",
    ],
)
def test_every_provider_replays_parallel_tool_calls_and_results(
    tmp_path, source, destination, persisted
):
    history = _tool_history(source)
    if persisted:
        history = _resume(tmp_path, history)
    original = deepcopy(history)

    calls, results = _converted_tool_exchange(destination, history)

    # Gemini and Ollama pair results by function name on the wire. The shared
    # history retains IDs, including the distinct Responses item/call IDs.
    expected_calls = [
        (name if destination in ("gemini", "ollama") else call_id, name, args)
        for call_id, name, args in CALLS
    ]
    assert calls == expected_calls
    assert results == [
        (call_id, f"result {name}") for call_id, name, _ in expected_calls
    ]
    assert history == original


@pytest.mark.asyncio
async def test_ollama_response_persists_generated_call_ids_for_provider_switch(
    tmp_path,
):
    # The native Ollama response has neither call IDs nor the output envelope
    # used by the conversation engine to save assistant tool declarations.
    data = {
        "message": {
            "content": "checking",
            "tool_calls": [
                {"function": {"name": name, "arguments": args}}
                for _, name, args in CALLS
            ],
        }
    }
    response = Mock(status_code=200)
    response.json.return_value = data
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    provider = OllamaProvider(base_url="https://example.invalid", model="test")

    with patch(
        "kerkov.providers.ollama_provider.httpx.AsyncClient", return_value=client
    ):
        completion = await provider.complete([Message(role="user", content="inspect")])

    history = [
        Message(
            role="assistant",
            content=completion.content,
            raw_blocks=extract_raw_blocks(completion.raw_response),
        ),
        *[
            Message(
                role="tool", content=f"result {call.tool_name}", call_id=call.call_id
            )
            for call in completion.tool_calls
        ],
    ]
    resumed = _resume(tmp_path, history)
    calls, results = _converted_tool_exchange("nvidia", resumed)

    ids = [call.call_id for call in completion.tool_calls]
    assert all(ids)
    assert len(set(ids)) == len(CALLS)
    assert calls == [
        (call_id, name, args) for call_id, (_, name, args) in zip(ids, CALLS)
    ]
    assert results == [(call_id, f"result {name}") for call_id, name, _ in calls]
