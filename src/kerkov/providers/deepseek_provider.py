"""DeepSeek provider using its OpenAI-compatible chat API."""

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx

from kerkov.models.basic_models import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
)
from kerkov.providers.base_provider import BaseLLMProvider

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


_SCHEMA_DROP_KEYS = {
    "$defs",
    "$schema",
    "definitions",
    "title",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}
_MODEL_LIST_TIMEOUT_SECONDS = 10


class DeepSeekProvider(BaseLLMProvider):
    """Hosted inference through DeepSeek API endpoints."""

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
            "Initialized DeepSeekProvider: "
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
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [self._convert_tool_schema(tool) for tool in tools]
            payload["tool_choice"] = "auto"
            # Thinking with tools requires reasoning history that other providers
            # cannot supply. Keep imported conversations usable without it.
            if any(
                message["role"] == "assistant" and "reasoning_content" not in message
                for message in payload["messages"]
            ):
                payload["thinking"] = {"type": "disabled"}
                for message in payload["messages"]:
                    message.pop("reasoning_content", None)

        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self.timeout_seconds,
        ) as client:
            response = await client.post("/chat/completions", json=payload)
            self._raise_for_status(response)
            data = response.json()

        return self._parse_completion_response(data)

    def validate_connection(self) -> bool:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get("/models")
                self._raise_for_status(response)
            return True
        except Exception as exc:
            self.logger.error(f"DeepSeek connection validation failed: {exc}")
            return False

    def list_models(self) -> List[str]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=min(self.timeout_seconds, _MODEL_LIST_TIMEOUT_SECONDS),
            ) as client:
                response = client.get("/models")
                self._raise_for_status(response)
                data = response.json()
            return [item["id"] for item in data.get("data", []) if item.get("id")]
        except Exception as exc:
            self.logger.error(f"Failed to list DeepSeek models: {exc}")
            return []

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
        usage = None
        if usage_data:
            prompt_tokens = int(usage_data.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage_data.get("completion_tokens", 0) or 0)
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=int(
                    usage_data.get("total_tokens") or prompt_tokens + completion_tokens
                ),
            )

        raw_response = {"output": [raw_message]} if raw_message else data
        return LLMResponse(
            content=content,
            model=data.get("model") or self.model,
            usage=usage,
            tool_calls=tool_calls or None,
            raw_response=raw_response,
        )

    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "assistant" and msg.raw_blocks:
                raw_messages = self._raw_blocks_to_messages(msg)
                if raw_messages:
                    converted.extend(raw_messages)
                    continue

            if msg.role == "tool":
                if msg.call_id:
                    converted.append(
                        {
                            "role": "tool",
                            "content": msg.content,
                            "tool_call_id": msg.call_id,
                        }
                    )
                else:
                    converted.append(
                        {
                            "role": "user",
                            "content": f"Tool result:\n{msg.content}",
                        }
                    )
            else:
                converted.append({"role": msg.role, "content": msg.content})
        return converted

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
                    "content": "\n".join(text_parts) or msg.content,
                    "tool_calls": tool_calls,
                }
            )
        elif text_parts:
            converted.append({"role": "assistant", "content": "\n".join(text_parts)})
        return converted

    def _sanitize_assistant_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        content = self._content_to_text(message.get("content"))
        sanitized: Dict[str, Any] = {"role": "assistant", "content": content}
        if isinstance(message.get("reasoning_content"), str):
            sanitized["reasoning_content"] = message["reasoning_content"]
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
                "arguments": self._arguments_to_json(function.get("arguments", {})),
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
        schema = tool.get_generic_schema().model_dump()
        parameters = schema.get("parameters", {})
        defs = parameters.get("$defs", {}) if isinstance(parameters, dict) else {}
        return {
            "type": "function",
            "function": {
                "name": schema.get("name"),
                "description": schema.get("description"),
                "parameters": self._clean_schema(parameters, defs),
            },
        }

    def _parse_tool_calls(self, raw_calls: List[Dict[str, Any]]) -> List[ToolCall]:
        parsed: List[ToolCall] = []
        for raw_call in raw_calls:
            function = raw_call.get("function", {})
            name = function.get("name")
            if not name:
                continue
            arguments = self._arguments_to_dict(function.get("arguments", {}))
            parsed.append(
                ToolCall(
                    tool_name=name,
                    parameters=arguments,
                    call_id=raw_call.get("id"),
                )
            )
        return parsed

    def _clean_schema(self, node: Any, defs: Dict[str, Any]) -> Any:
        if isinstance(node, list):
            return [self._clean_schema(item, defs) for item in node]
        if not isinstance(node, dict):
            return node

        schema_node = dict(node)
        if "$ref" in schema_node:
            schema_node = self._resolve_schema_ref(schema_node, defs)

        cleaned: Dict[str, Any] = {}
        for key, value in schema_node.items():
            if key in _SCHEMA_DROP_KEYS:
                continue
            if key == "properties":
                cleaned[key] = {
                    name: self._clean_schema(schema, defs)
                    for name, schema in value.items()
                }
            else:
                cleaned[key] = self._clean_schema(value, defs)
        return cleaned

    @staticmethod
    def _resolve_schema_ref(
        schema_node: Dict[str, Any], defs: Dict[str, Any]
    ) -> Dict[str, Any]:
        def_name = schema_node["$ref"].split("/")[-1]
        resolved = dict(defs.get(def_name, {}))
        for key, value in schema_node.items():
            if key != "$ref":
                resolved[key] = value
        return resolved

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

    def _arguments_to_dict(self, arguments: Any) -> Dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str) and arguments.strip():
            try:
                parsed = json.loads(arguments)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                self.logger.warning("Invalid DeepSeek tool arguments returned")
        return {}

    @staticmethod
    def _arguments_to_json(arguments: Any) -> str:
        if isinstance(arguments, str):
            return arguments
        return json.dumps(arguments or {})

    @staticmethod
    def _fallback_call_id(raw_call: Dict[str, Any]) -> str:
        function = raw_call.get("function", {})
        name = (
            function.get("name") if isinstance(function, dict) else raw_call.get("name")
        )
        payload = json.dumps(raw_call, sort_keys=True, default=str)
        return f"call_{abs(hash(payload))}_" f"{name or 'tool'}"

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 402:
            raise ValueError(
                "DeepSeek API error 402: Insufficient balance. "
                "Please top up your DeepSeek account."
            )
        detail = response.text
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error", {})
            detail = error.get("message", error) if isinstance(error, dict) else error
        raise ValueError(f"DeepSeek API error {response.status_code}: {detail}")
