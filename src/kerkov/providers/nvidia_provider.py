"""NVIDIA NIM provider using its OpenAI-compatible chat API."""

import json
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import httpx

from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.openai_stream import reconstruct_openai_stream

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


class NvidiaProvider(BaseLLMProvider):
    """Hosted inference through NVIDIA NIM API endpoints."""

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
            f"Initialized NvidiaProvider: model='{model}', base_url='{self.base_url}'"
        )

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

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

    def _parse_completion_response(self, data: Dict[str, Any]) -> LLMResponse:
        choices = data.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content") or ""
        tool_calls = self._parse_tool_calls(message.get("tool_calls", []))

        usage_data = data.get("usage") or {}
        usage = None
        if usage_data:
            usage = TokenUsage(
                prompt_tokens=int(usage_data.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage_data.get("completion_tokens", 0) or 0),
                total_tokens=int(usage_data.get("total_tokens", 0) or 0),
            )

        # Preserve the assistant's function calls so subsequent tool results can
        # be replayed in the OpenAI-compatible message format.
        raw_response = {"output": [message]} if message else data
        return LLMResponse(
            content=content,
            model=data.get("model") or self.model,
            usage=usage,
            tool_calls=tool_calls or None,
            raw_response=raw_response,
        )

    async def _raise_for_stream_status(self, response: httpx.Response) -> None:
        """Read the body of a failed streaming response, then raise as usual."""
        if response.status_code >= 400:
            await response.aread()
            self._raise_for_status(response)

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
            self.logger.error(f"NVIDIA NIM connection validation failed: {exc}")
            return False

    def list_models(self) -> List[str]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                headers=self._headers,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get("/models")
                self._raise_for_status(response)
                data = response.json()
            return [item["id"] for item in data.get("data", []) if item.get("id")]
        except Exception as exc:
            self.logger.error(f"Failed to list NVIDIA NIM models: {exc}")
            return []

    async def get_max_context_tokens(self) -> Optional[int]:
        return None

    async def get_max_output_tokens(self) -> Optional[int]:
        return None

    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        converted: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "assistant" and msg.raw_blocks:
                raw = msg.raw_blocks[0]
                if isinstance(raw, dict) and raw.get("tool_calls"):
                    assistant = {
                        "role": "assistant",
                        "content": msg.content or None,
                        "tool_calls": raw["tool_calls"],
                    }
                    converted.append(assistant)
                    continue
            if msg.role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "content": msg.content,
                        "tool_call_id": msg.call_id,
                    }
                )
            else:
                converted.append({"role": msg.role, "content": msg.content})
        return converted

    def _convert_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        schema = tool.get_generic_schema().model_dump()
        return {
            "type": "function",
            "function": {
                "name": schema.get("name"),
                "description": schema.get("description"),
                "parameters": schema.get("parameters", {}),
            },
        }

    def _parse_tool_calls(self, raw_calls: List[Dict[str, Any]]) -> List[ToolCall]:
        parsed: List[ToolCall] = []
        for raw_call in raw_calls:
            function = raw_call.get("function", {})
            name = function.get("name")
            if not name:
                continue
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    self.logger.warning(f"Invalid tool arguments returned for '{name}'")
                    arguments = {}
            parsed.append(
                ToolCall(
                    tool_name=name,
                    parameters=arguments or {},
                    call_id=raw_call.get("id"),
                )
            )
        return parsed

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text
        try:
            error = response.json().get("error", {})
            detail = error.get("message", error) if isinstance(error, dict) else error
        except Exception:
            pass
        raise ValueError(f"NVIDIA NIM API error {response.status_code}: {detail}")
