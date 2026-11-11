"""Shared base for providers that speak the OpenAI chat-completions format."""

import json
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional

import httpx

from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.openai_stream import reconstruct_openai_stream
from kerkov.providers.tool_arguments import parse_tool_arguments

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


class OpenAICompatibleProvider(BaseLLMProvider):
    """Chat-completions transport, tool calls, replay and error handling."""

    # Names the service in API error messages.
    api_label: ClassVar[str] = "OpenAI-compatible"
    # What an assistant turn without text sends as `content`.
    empty_assistant_content: ClassVar[Optional[str]] = None
    # Whether `reasoning_content` is replayed on later requests.
    replays_reasoning: ClassVar[bool] = False

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 60,
    ) -> None:
        super().__init__(api_key=api_key, model=model)
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.logger.info(
            f"Initialized {type(self).__name__}: "
            f"model='{model}', base_url='{self.base_url}'"
        )

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        streaming = on_delta is not None
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "stream": streaming,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [self._convert_tool_schema(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        if streaming:
            payload["stream_options"] = {"include_usage": True}
        self._adjust_payload(payload)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self.timeout_seconds,
        ) as client:
            if streaming:
                async with client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    await self._raise_for_stream_status(response)
                    data = await reconstruct_openai_stream(
                        response.aiter_lines(), on_delta
                    )
            else:
                response = await client.post("/chat/completions", json=payload)
                self._raise_for_status(response)
                data = response.json()

        return self._parse_completion_response(data)

    def _adjust_payload(self, payload: Dict[str, Any]) -> None:
        """Apply provider-specific request changes before sending."""

    async def get_max_context_tokens(self) -> Optional[int]:
        return None

    async def get_max_output_tokens(self) -> Optional[int]:
        return None

    def _parse_completion_response(self, data: Dict[str, Any]) -> LLMResponse:
        choices = data.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        content = self._content_to_text(message.get("content"))
        raw_message = self._assistant_message_for_replay(message, content)
        tool_calls = self._parse_tool_calls(raw_message.get("tool_calls", []))
        usage_data = data.get("usage") or {}

        # The sanitized message is the replay record: later turns send its tool
        # calls back so their results can be paired by id.
        raw_response = {"output": [raw_message]} if raw_message else data
        return LLMResponse(
            content=content,
            model=data.get("model") or self.model,
            usage=self._parse_usage(usage_data) if usage_data else None,
            tool_calls=tool_calls or None,
            raw_response=raw_response,
        )

    def _parse_usage(self, usage_data: Dict[str, Any]) -> TokenUsage:
        prompt_tokens = int(usage_data.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage_data.get("completion_tokens", 0) or 0)
        prompt_details = usage_data.get("prompt_tokens_details") or {}
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(
                usage_data.get("total_tokens") or prompt_tokens + completion_tokens
            ),
            cache_read_tokens=int(self._cache_read_tokens(usage_data) or 0),
            cache_creation_tokens=int(prompt_details.get("cache_write_tokens", 0) or 0),
        )

    def _cache_read_tokens(self, usage_data: Dict[str, Any]) -> Any:
        return (usage_data.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            converted.extend(self._convert_message(msg))
        return converted

    def _convert_message(self, msg: Message) -> List[Dict[str, Any]]:
        if msg.role == "assistant" and msg.raw_blocks:
            raw_messages = self._raw_blocks_to_messages(msg)
            if raw_messages:
                return raw_messages
        if msg.role == "tool":
            if msg.call_id:
                return [
                    {
                        "role": "tool",
                        "content": msg.content,
                        "tool_call_id": msg.call_id,
                    }
                ]
            return [{"role": "user", "content": f"Tool result:\n{msg.content}"}]
        return [{"role": msg.role, "content": msg.content}]

    def _raw_blocks_to_messages(self, msg: Message) -> List[Dict[str, Any]]:
        if not isinstance(msg.raw_blocks, list):
            return []

        converted: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        for block in msg.raw_blocks:
            if hasattr(block, "model_dump"):
                block = block.model_dump()
            if not isinstance(block, dict):
                continue

            if block.get("role") == "assistant":
                converted.append(self._sanitize_assistant_message(block))
                continue

            if block.get("type") in ("text", "output_text") and block.get("text"):
                text_parts.append(str(block["text"]))
                continue

            converted_tool_call = self._cross_provider_block_to_tool_call(block)
            if converted_tool_call:
                tool_calls.append(converted_tool_call)

        if tool_calls:
            converted.append(
                {
                    "role": "assistant",
                    "content": "\n".join(text_parts)
                    or msg.content
                    or self.empty_assistant_content,
                    "tool_calls": tool_calls,
                }
            )
        elif text_parts:
            converted.append({"role": "assistant", "content": "\n".join(text_parts)})
        return converted

    def _sanitize_assistant_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        content = self._content_to_text(message.get("content"))
        sanitized: Dict[str, Any] = {
            "role": "assistant",
            "content": content or self.empty_assistant_content,
        }
        reasoning = message.get("reasoning_content")
        if self.replays_reasoning and isinstance(reasoning, str):
            sanitized["reasoning_content"] = reasoning
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            sanitized["tool_calls"] = [
                self._sanitize_tool_call(raw_call)
                for raw_call in tool_calls
                if isinstance(raw_call, dict)
            ]
        return sanitized

    def _assistant_message_for_replay(
        self, message: Dict[str, Any], content: str
    ) -> Dict[str, Any]:
        if not message:
            return {}
        return self._sanitize_assistant_message(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": message.get("tool_calls", []),
                "reasoning_content": message.get("reasoning_content"),
            }
        )

    def _sanitize_tool_call(self, raw_call: Dict[str, Any]) -> Dict[str, Any]:
        function = raw_call.get("function", {})
        if not isinstance(function, dict):
            function = {}
        return {
            "id": raw_call.get("id") or self._fallback_call_id(raw_call),
            "type": "function",
            "function": {
                "name": function.get("name") or "",
                "arguments": self._arguments_to_json(function.get("arguments")),
            },
        }

    def _cross_provider_block_to_tool_call(
        self, block: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if block.get("type") not in ("function_call", "tool_use"):
            return None
        name = block.get("name")
        if not name:
            return None
        arguments = (
            block.get("arguments")
            if "arguments" in block
            else block.get("args", block.get("input", {}))
        )
        return {
            "id": block.get("call_id")
            or block.get("id")
            or self._fallback_call_id(block),
            "type": "function",
            "function": {
                "name": name,
                "arguments": self._arguments_to_json(arguments),
            },
        }

    def _convert_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        schema = tool.get_generic_schema()
        return {
            "type": "function",
            "function": {
                "name": schema.name,
                "description": schema.description,
                "parameters": schema.parameters,
            },
        }

    def _parse_tool_calls(self, raw_calls: List[Dict[str, Any]]) -> List[ToolCall]:
        parsed: List[ToolCall] = []
        for raw_call in raw_calls:
            function = raw_call.get("function", {})
            name = function.get("name")
            if not name:
                continue
            arguments = parse_tool_arguments(function.get("arguments"))
            parsed.append(
                ToolCall(
                    tool_name=name,
                    parameters=arguments.parameters,
                    argument_error=arguments.error,
                    call_id=raw_call.get("id"),
                )
            )
        return parsed

    async def _raise_for_stream_status(self, response: httpx.Response) -> None:
        """Read the body of a failed streaming response, then raise as usual."""
        if response.status_code >= 400:
            await response.aread()
            self._raise_for_status(response)

    @classmethod
    def _raise_for_status(cls, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error", {})
            detail = error.get("message", error) if isinstance(error, dict) else error
        raise ValueError(f"{cls.api_label} API error {response.status_code}: {detail}")

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            return str(text) if text else ""
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        return str(content)

    @staticmethod
    def _arguments_to_json(arguments: Any) -> str:
        if isinstance(arguments, str):
            return arguments
        # Only absent arguments mean `{}`. A falsy non-object such as `[]` must
        # stay visible so the tool-call parser reports it instead of running
        # the tool on its defaults.
        return json.dumps({} if arguments is None else arguments)

    @staticmethod
    def _fallback_call_id(raw_call: Dict[str, Any]) -> str:
        function = raw_call.get("function", {})
        name = (
            function.get("name") if isinstance(function, dict) else raw_call.get("name")
        )
        payload = json.dumps(raw_call, sort_keys=True, default=str)
        return f"call_{abs(hash(payload))}_{name or 'tool'}"
