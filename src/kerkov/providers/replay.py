"""Portable tool calls from the raw history shared by provider switches.

Providers keep native reasoning in their own replay paths. Only function calls
are translated here; opaque reasoning/signatures must not cross API formats.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class ReplayToolCall:
    call_id: str
    name: str
    arguments: Any

    def arguments_json(self) -> str:
        if isinstance(self.arguments, str):
            return self.arguments
        return json.dumps(self.arguments or {})

    def arguments_dict(self) -> Dict[str, Any]:
        arguments = self.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return {}
        return arguments if isinstance(arguments, dict) else {}

    def chat_call(self) -> Dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_json()},
        }


def replay_tool_calls(raw_blocks: Any) -> List[ReplayToolCall]:
    """Read Anthropic SDK/dict, Responses, Gemini, and chat completion calls."""
    calls: List[ReplayToolCall] = []
    if not isinstance(raw_blocks, list):
        return calls
    for block in raw_blocks:
        if hasattr(block, "model_dump"):
            block = block.model_dump()
        if not isinstance(block, dict):
            continue
        if block.get("role") == "assistant":
            for raw_call in block.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    continue
                if raw_call.get("id") and function.get("name"):
                    calls.append(
                        ReplayToolCall(
                            raw_call["id"],
                            function["name"],
                            function.get("arguments", {}),
                        )
                    )
        elif block.get("type") in ("function_call", "tool_use"):
            call_id = block.get("call_id") or block.get("id")
            if call_id and block.get("name"):
                calls.append(
                    ReplayToolCall(
                        call_id,
                        block["name"],
                        block.get(
                            "arguments", block.get("args", block.get("input", {}))
                        ),
                    )
                )
    return calls
