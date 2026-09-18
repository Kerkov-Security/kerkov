"""DeepSeek requests through the shared engine, including streamed replay."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from kerkov.context.token_tracker import TokenTracker
from kerkov.core.approval_scope import ApprovalScope
from kerkov.core.conversation_engine import (
    ConversationEngine,
    LoopSession,
    QuietSink,
    WhitelistAutoApproval,
    extract_raw_blocks,
)
from kerkov.models.basic_models import Message
from kerkov.providers.deepseek_provider import DeepSeekProvider
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult


class _InspectParams(ToolParams):
    path: str


class _InspectTool(BaseTool):
    def __init__(self):
        super().__init__("inspect", "Inspect a file")

    def get_params_class(self):
        return _InspectParams

    def validate_params(self, params):
        return None

    def approval_scope(self, params):
        return ApprovalScope.exact_only()

    async def run(self, params):
        return ToolResult(success=True, output=params.path)


class _Sink(QuietSink):
    def __init__(self, streaming):
        self.streaming = streaming
        self.deltas = []
        self.started = 0
        self.ended = 0

    @property
    def supports_streaming(self):
        return self.streaming

    def assistant_delta(self, text):
        self.deltas.append(text)

    def begin_assistant_stream(self):
        self.started += 1

    def end_assistant_stream(self):
        self.ended += 1


def _provider():
    return DeepSeekProvider("test-key", "https://api.deepseek.com", "deepseek-flash")


def _sse_response(deltas, usage, finish_reason):
    chunks = [
        {
            "model": "deepseek-flash",
            "choices": [{"delta": delta, "finish_reason": None}],
            "usage": None,
        }
        for delta in deltas
    ]
    # DeepSeek supplies usage on its final choices-bearing chunk.
    chunks.append(
        {
            "choices": [{"delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
        }
    )
    content = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200,
        content=(content + "data: [DONE]\n\n").encode(),
        headers={"Content-Type": "text/event-stream"},
    )


@pytest.mark.parametrize("streaming", [False, True])
def test_engine_preserves_reasoning_tool_replay_and_cache_prefix(streaming):
    requests = []
    usage = {
        "prompt_tokens": 1100,
        "completion_tokens": 100,
        "total_tokens": 1200,
        "prompt_cache_hit_tokens": 1000,
        "prompt_cache_miss_tokens": 100,
    }
    tool_call = {
        "id": "call_inspect",
        "type": "function",
        "function": {"name": "inspect", "arguments": '{"path":"README.md"}'},
    }
    first_message = {
        "role": "assistant",
        "content": "Checking now",
        "reasoning_content": "Inspect the file.",
        "tool_calls": [tool_call],
    }

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["stream"] is streaming
        assert "thinking" not in payload
        if streaming:
            assert payload["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in payload
        if len(requests) == 1:
            if streaming:
                return _sse_response(
                    [
                        {"reasoning_content": "Inspect "},
                        {"reasoning_content": "the file."},
                        {"content": "Checking"},
                        {"content": " now"},
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_inspect",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": '{"path":',
                                    },
                                }
                            ]
                        },
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"README.md"}'},
                                }
                            ]
                        },
                    ],
                    usage,
                    "tool_calls",
                )
            return httpx.Response(
                200, json={"choices": [{"message": first_message}], "usage": usage}
            )
        if streaming:
            return _sse_response(
                [{"reasoning_content": "File read."}, {"content": "Done"}],
                usage,
                "stop",
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "Done", "reasoning_content": "File read."}}
                ],
                "usage": usage,
            },
        )

    sink = _Sink(streaming)
    session = LoopSession(
        provider=_provider(),
        history=[
            Message(role="system", content="System"),
            Message(role="user", content="Read"),
        ],
        tools=[_InspectTool()],
        approval=WhitelistAutoApproval(),
        output=sink,
        max_iterations=3,
        token_tracker=TokenTracker(),
        ledger_block="Ledger revision 1",
    )

    async def conversation():
        engine = ConversationEngine()
        response = await engine._complete(session)
        assert response.content == "Checking now"
        assert response.raw_response["output"] == [first_message]
        assert response.tool_calls[0].parameters == {"path": "README.md"}
        assert response.usage.prompt_tokens == 1100
        assert response.usage.total_tokens == 1200
        assert response.usage.cache_read_tokens == 1000
        assert response.usage.cache_creation_tokens == 0
        session.history.extend(
            [
                Message(
                    role="assistant",
                    content=response.content,
                    raw_blocks=extract_raw_blocks(response.raw_response),
                ),
                Message(role="tool", content="File contents", call_id="call_inspect"),
            ]
        )
        session.ledger_block = "Ledger revision 2"
        await engine._complete(session)

    client_type = httpx.AsyncClient
    with patch(
        "kerkov.providers.deepseek_provider.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ):
        asyncio.run(conversation())

    assert requests[0]["messages"][-1] == {
        "role": "system",
        "content": "Ledger revision 1",
    }
    assert requests[1]["messages"][-1] == {
        "role": "system",
        "content": "Ledger revision 2",
    }
    assert requests[1]["messages"][:2] == requests[0]["messages"][:2]
    assert requests[1]["messages"][2] == first_message
    assert requests[1]["messages"][3]["tool_call_id"] == "call_inspect"
    assert all("Ledger revision" not in msg.content for msg in session.history)
    assert sink.deltas == (["Checking", " now", "Done"] if streaming else [])
    assert sink.started == sink.ended == (2 if streaming else 0)


@pytest.mark.parametrize(
    "status, detail", [(401, "Invalid key"), (402, "Insufficient balance")]
)
def test_streaming_reads_error_body_before_reporting_api_error(status, detail):
    def respond(request):
        return httpx.Response(status, json={"error": {"message": "Invalid key"}})

    client_type = httpx.AsyncClient
    with patch(
        "kerkov.providers.deepseek_provider.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    ):
        with pytest.raises(
            ValueError, match=f"DeepSeek API error {status}: .*{detail}"
        ):
            asyncio.run(
                _provider().complete(
                    [Message(role="user", content="Hello")], on_delta=lambda text: None
                )
            )
