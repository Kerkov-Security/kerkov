import json
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import httpx

from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.replay import replay_tool_calls

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


class OllamaProvider(BaseLLMProvider):
    """Ollama LLM provider."""

    def __init__(self, base_url: str, model: str, timeout_seconds: int = 60):
        """
        Initialize the Ollama provider.

        Args:
            base_url: Base URL for the Ollama server
            model: Model name (e.g., llama3)
            timeout_seconds: Request timeout in seconds
        """
        super().__init__(api_key="", model=model)
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._context_window_cache: Dict[str, Optional[int]] = {}
        self.logger.info(
            f"Initialized OllamaProvider: model='{model}', "
            f"base_url='{self.base_url}', timeout={timeout_seconds}s"
        )

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """
        Generate a completion from Ollama.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature. When None, the model's own
                default sampling is used.
            max_tokens: Maximum tokens to generate
            tools: Optional list of tools (not used by Ollama generate API)

        Returns:
            LLMResponse object with the completion
        """
        self.logger.info(
            f"complete() called: model={self.model}, "
            f"messages={len(messages)}, temp={temperature}, "
            f"max_tokens={max_tokens}, tools={len(tools) if tools else 0}"
        )

        ollama_messages = self._convert_messages_to_chat(messages)

        options: Dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature

        streaming = on_delta is not None
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": streaming,
            "options": options,
        }

        if tools:
            payload["tools"] = [
                self._convert_to_ollama_tool_schema(tool) for tool in tools
            ]

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        ) as client:
            if streaming:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        self._raise_ollama_error(response)
                    data = await self._reconstruct_chat_stream(
                        response.aiter_lines(), on_delta
                    )
            else:
                response = await client.post("/api/chat", json=payload)
                if response.status_code >= 400:
                    self._raise_ollama_error(response)
                data = response.json()

        message = data.get("message", {}) if isinstance(data, dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""

        usage = None
        if "prompt_eval_count" in data or "eval_count" in data:
            prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
            completion_tokens = int(data.get("eval_count", 0) or 0)
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )

        tool_calls = None
        if isinstance(message, dict) and message.get("tool_calls"):
            tool_calls = []
            for tool_call in message.get("tool_calls", []):
                function_call = tool_call.get("function", {})
                name = function_call.get("name")
                if not name:
                    continue
                # Ollama usually omits IDs. Persist the same local ID that the
                # engine puts on tool results so another provider can pair them.
                tool_call["id"] = tool_call.get("id") or f"call_{uuid.uuid4().hex}"
                arguments = function_call.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Failed to parse tool call arguments as JSON for '{name}'"
                        )
                        arguments = {}
                tool_calls.append(
                    ToolCall(
                        tool_name=name,
                        parameters=arguments or {},
                        call_id=tool_call.get("id"),
                    )
                )

        if not content and not tool_calls:
            self.logger.warning(
                "Ollama returned empty response (no content or tool calls)"
            )
            content = "I apologize, but I was unable to generate a proper response. Please try again."

        self.logger.info(
            f"complete() response: tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"content_length={len(content)}, usage={usage}"
        )

        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            tool_calls=tool_calls,
            raw_response=(
                {**data, "output": [{"role": "assistant", **message}]}
                if message
                else data
            ),
        )

    def _raise_ollama_error(self, response: httpx.Response) -> None:
        detail = response.text
        try:
            detail_json = response.json()
            detail = detail_json.get("error", detail)
        except Exception:
            pass

        if response.status_code == 404 and "model" in detail.lower():
            self.logger.error(f"Model '{self.model}' not found on Ollama server")
            raise ValueError(
                f"Ollama model '{self.model}' not found. Run: ollama pull {self.model}"
            )

        self.logger.error(
            f"Ollama API error: status={response.status_code}, detail={detail}"
        )
        raise ValueError(
            f"Ollama API error {response.status_code} at {response.request.url}: {detail}"
        )

    async def _reconstruct_chat_stream(
        self, lines: Any, on_delta: Optional[Callable[[str], None]]
    ) -> Dict[str, Any]:
        """Fold Ollama's NDJSON chat stream into a single aggregated response."""
        content_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        final: Dict[str, Any] = {}
        async for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Ollama reports mid-stream failures as a 200 NDJSON object carrying
            # `error` and no `message`/`done`; skipping it would return the
            # truncated content so far as a successful answer.
            if chunk.get("error"):
                raise ValueError(f"Ollama streaming error: {chunk['error']}")
            message = chunk.get("message") or {}
            fragment = message.get("content") or ""
            if fragment:
                content_parts.append(fragment)
                if on_delta is not None:
                    on_delta(fragment)
            if message.get("tool_calls"):
                tool_calls.extend(message["tool_calls"])
            if chunk.get("done"):
                final = chunk

        data = dict(final)
        aggregated: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if tool_calls:
            aggregated["tool_calls"] = tool_calls
        data["message"] = aggregated
        return data

    async def get_max_output_tokens(self) -> Optional[int]:
        return None

    async def get_max_context_tokens(self) -> Optional[int]:
        if self.model in self._context_window_cache:
            return self._context_window_cache[self.model]
        window: Optional[int] = None
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = await client.post("/api/show", json={"name": self.model})
                response.raise_for_status()
                data = response.json()

            model_info = data.get("model_info", {})
            for key, value in model_info.items():
                if "context_length" in key:
                    window = int(value)
                    break
        except Exception as e:
            self.logger.warning(f"Failed to retrieve model context window: {e}")
            window = None
        self._context_window_cache[self.model] = window
        return window

    def validate_connection(self) -> bool:
        """Validate chat support for the selected model, not just server access."""
        self.logger.info(f"Validating connection to Ollama at {self.base_url}")
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.post("/api/show", json={"name": self.model})
                response.raise_for_status()
                metadata = response.json()
                if not isinstance(metadata, dict) or metadata.get("error"):
                    return False
                if "capabilities" in metadata:
                    capabilities = metadata["capabilities"]
                    # /api/tags includes embedding models. Ollama's chat
                    # endpoint requires the completion capability.
                    return (
                        isinstance(capabilities, list)
                        and all(isinstance(value, str) for value in capabilities)
                        and "completion" in capabilities
                    )
                # Older Ollama releases omit capability metadata. Confirm chat
                # with a bounded probe that never includes engagement history.
                response = client.post(
                    "/api/chat",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "Hi"}],
                        "stream": False,
                        "options": {"num_predict": 1},
                    },
                )
                response.raise_for_status()
                result = response.json()
                return (
                    isinstance(result, dict)
                    and not result.get("error")
                    and result.get("done") is True
                    and isinstance(result.get("message"), dict)
                    and result["message"].get("role") == "assistant"
                )
        except Exception as exc:
            self.logger.error(
                "Ollama connection validation failed (%s)", type(exc).__name__
            )
            return False

    def list_models(self) -> List[str]:
        """
        List available Ollama models.

        Returns:
            List of model names
        """
        self.logger.info("Listing available Ollama models")
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                data = response.json()
            models = [m.get("name") for m in data.get("models", []) if m.get("name")]
            self.logger.info(f"Found {len(models)} models")
            return models
        except Exception as e:
            self.logger.error(f"Failed to list models: {e}")
            return []

    def _convert_messages_to_chat(
        self, messages: List[Message]
    ) -> List[Dict[str, Any]]:
        """
        Convert standard messages to Ollama chat format.

        Args:
            messages: List of standard Message objects

        Returns:
            List of Ollama-formatted message dictionaries
        """
        chat_messages: List[Dict[str, Any]] = []
        call_id_to_name: Dict[str, str] = {}
        for msg in messages:
            if msg.role == "system":
                chat_messages.append({"role": "system", "content": msg.content})
            elif msg.role == "user":
                chat_messages.append({"role": "user", "content": msg.content})
            elif msg.role == "assistant":
                assistant: Dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content,
                }
                calls = replay_tool_calls(msg.raw_blocks)
                if calls:
                    assistant["tool_calls"] = [
                        {
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments_dict(),
                            }
                        }
                        for call in calls
                    ]
                    call_id_to_name.update({call.call_id: call.name for call in calls})
                chat_messages.append(assistant)
            elif msg.role == "tool":
                result = {"role": "tool", "content": msg.content}
                name = call_id_to_name.get(msg.call_id or "")
                if name:
                    result["tool_name"] = name
                chat_messages.append(result)

        return chat_messages

    def _convert_to_ollama_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        generic_schema = tool.get_generic_schema()
        dict_of_schema = generic_schema.model_dump()
        return {
            "type": "function",
            "function": {
                "name": dict_of_schema.get("name"),
                "description": dict_of_schema.get("description"),
                "parameters": dict_of_schema.get("parameters", {}),
            },
        }
