"""Reassemble an OpenAI-compatible SSE chat stream into a final response dict.

NVIDIA and OpenRouter share the OpenAI chat-completions wire format. Rather than
grow a second parser for the streamed shape, this rebuilds the same
`{"choices": [{"message": ...}], "usage": ...}` dict the non-streaming path
returns, so each provider's existing `_parse_completion_response` handles tool
calls and usage unchanged. Content deltas are forwarded to `on_delta` as they
arrive.
"""

import json
from typing import Any, AsyncIterator, Callable, Dict, Optional


async def reconstruct_openai_stream(
    lines: AsyncIterator[str],
    on_delta: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Consume an SSE line iterator and return a non-streaming-shaped response."""
    content_parts = []
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    model: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None

    async for line in lines:
        if not line or not line.startswith("data:"):
            continue
        data_str = line[len("data:") :].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            text = delta.get("content")
            if text:
                content_parts.append(text)
                if on_delta is not None:
                    on_delta(text)

            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                slot = tool_calls_by_index.setdefault(
                    index,
                    {
                        "id": None,
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls_by_index:
        message["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index)
        ]

    data: Dict[str, Any] = {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }
    if model:
        data["model"] = model
    if usage:
        data["usage"] = usage
    return data
