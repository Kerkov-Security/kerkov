import asyncio
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import anthropic

from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers import BaseLLMProvider

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


_SCHEMA_DROP_KEYS = {"$defs", "$schema", "title", "definitions"}
_ASSISTANT_BLOCK_TYPES = {"text", "thinking", "redacted_thinking", "tool_use"}


class AnthropicProvider(BaseLLMProvider):

    def __init__(self, api_key: str, model: str) -> None:
        """
        Initialize the Gemini provider.

        Args:
            api_key: Google API key
            model: Model name (default: gemini-pro)
        """
        super().__init__(api_key, model)
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key)
        self._context_window_cache: Dict[str, Optional[int]] = {}
        self._max_output_cache: Dict[str, Optional[int]] = {}
        self.logger.info(f"Initialized AnthropicProvider with model '{model}'")

    @property
    def caches_prompt_prefix(self) -> bool:
        return True

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        self.logger.info(
            f"complete() called: model={self.model}, "
            f"messages={len(messages)}, temp={temperature}, "
            f"max_tokens={max_tokens}, tools={len(tools) if tools else 0}"
        )

        request = self._build_request(messages, temperature, max_tokens, tools)

        try:
            async with self.client.messages.stream(**request) as stream:
                if on_delta is not None:
                    async for text in stream.text_stream:
                        on_delta(text)
                message = await stream.get_final_message()
        except asyncio.CancelledError:
            self.logger.info("Anthropic stream cancelled by user")
            raise
        except Exception as e:
            self.logger.error(f"Anthropic API call failed: {e}")
            raise

        content = ""
        tool_calls = []
        for block in message.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        tool_name=block.name,
                        parameters=block.input,
                        call_id=block.id,
                    )
                )

        usage = self._usage_from_message(message)

        self.logger.info(
            f"complete() response: tool_calls={len(tool_calls)}, "
            f"content_length={len(content)}, usage={usage}"
        )

        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            raw_response=message,
        )

    def _build_request(
        self,
        messages: List[Message],
        temperature: Optional[float],
        max_tokens: Optional[int],
        tools: Optional[List["BaseTool"]],
    ) -> Dict[str, Any]:
        """Assemble the Messages API request with rolling cache breakpoints.

        Two ephemeral breakpoints follow the standard rolling pattern: one on
        the stable system+tools prelude, one on the last content block of the
        most recent turn. Mutable working state (the findings ledger, transient
        prompts) arrives as trailing `system` messages and is appended *after*
        the second breakpoint so it never enters the cached prefix.
        """
        work = list(messages)
        system_prompt = ""
        if work and work[0].role == "system":
            system_prompt = work[0].content
            work = work[1:]

        tail_start = len(work)
        while tail_start > 0 and work[tail_start - 1].role == "system":
            tail_start -= 1
        conversation = work[:tail_start]
        volatile_tail = work[tail_start:]

        anthropic_messages = self._convert_messages(conversation)
        anthropic_tools = (
            [self._convert_to_anthropic_tool_schema(tool) for tool in tools]
            if tools
            else []
        )

        system = self._system_with_cache(system_prompt, anthropic_tools)
        self._mark_last_turn_cache_breakpoint(anthropic_messages)
        self._append_volatile_tail(anthropic_messages, volatile_tail)

        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "tools": anthropic_tools,
            "system": system,
            "messages": anthropic_messages,
        }
        if temperature is not None:
            request["temperature"] = temperature
        return request

    def _system_with_cache(
        self, system_prompt: str, anthropic_tools: List[Dict[str, Any]]
    ) -> Any:
        """Cache the tool schemas + stable system prelude behind one breakpoint.

        Anthropic renders tools then system, so a breakpoint on the last system
        block caches both. With no system prompt, fall back to a breakpoint on
        the last tool so the tool prelude still caches; with neither, there is
        nothing stable to cache. Below the model's minimum cacheable prefix
        Anthropic simply ignores the directive — no error, no cache write.
        """
        if system_prompt:
            return [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if anthropic_tools:
            anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
        return ""

    def _mark_last_turn_cache_breakpoint(
        self, anthropic_messages: List[Dict[str, Any]]
    ) -> None:
        """Roll the second breakpoint onto the last block of the last turn."""
        if not anthropic_messages:
            return
        last = anthropic_messages[-1]
        blocks = self._as_block_list(last["content"])
        if not blocks or not isinstance(blocks[-1], dict):
            return
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = blocks

    def _append_volatile_tail(
        self, anthropic_messages: List[Dict[str, Any]], volatile_tail: List[Message]
    ) -> None:
        """Append mutable working state after the cached prefix.

        Merged into the trailing user turn when there is one, so no breakpoint
        covers it and no consecutive same-role turn is produced.
        """
        text = "\n\n".join(m.content for m in volatile_tail if m.content)
        if not text:
            return
        block = {"type": "text", "text": text}
        if anthropic_messages and anthropic_messages[-1]["role"] == "user":
            blocks = self._as_block_list(anthropic_messages[-1]["content"])
            blocks.append(block)
            anthropic_messages[-1]["content"] = blocks
        else:
            anthropic_messages.append({"role": "user", "content": [block]})

    @staticmethod
    def _as_block_list(content: Any) -> List[Any]:
        if isinstance(content, list):
            return list(content)
        return [{"type": "text", "text": content}]

    def _usage_from_message(self, message: Any) -> Optional[TokenUsage]:
        """Sum uncached + cached input so the tracker sees the full prompt size.

        With caching on, `usage.input_tokens` is only the uncached remainder;
        the authoritative prompt size adds the cache read and cache write counts.
        """
        if not message.usage:
            return None
        input_tokens = message.usage.input_tokens or 0
        cache_read = getattr(message.usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(message.usage, "cache_creation_input_tokens", 0) or 0
        prompt_tokens = input_tokens + cache_read + cache_creation
        completion_tokens = message.usage.output_tokens or 0
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
        )

    def _convert_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        anthropic_messages: List[Dict[str, Any]] = []
        pending_tool_results: List[Dict[str, Any]] = []

        for msg in messages:
            if msg.role == "tool":
                pending_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.call_id,
                        "content": msg.content,
                    }
                )
            else:
                if pending_tool_results:
                    anthropic_messages.append(
                        {
                            "role": "user",
                            "content": pending_tool_results,
                        }
                    )
                    pending_tool_results = []

                if msg.role in ("user", "system"):
                    if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                        prev = anthropic_messages[-1]["content"]
                        if isinstance(prev, str):
                            anthropic_messages[-1]["content"] = (
                                prev + "\n\n" + msg.content
                            )
                        else:
                            # prev is a list (tool_results block), start a new user turn
                            anthropic_messages.append(
                                {"role": "user", "content": msg.content}
                            )
                    else:
                        anthropic_messages.append(
                            {"role": "user", "content": msg.content}
                        )
                elif msg.role == "assistant":
                    # Raw history is shared across provider switches. Preserve
                    # native content blocks, but use portable text for a foreign
                    # provider's message/output envelope.
                    content: Any = msg.content
                    if isinstance(msg.raw_blocks, list) and msg.raw_blocks:
                        if all(
                            (
                                block.get("type")
                                if isinstance(block, dict)
                                else getattr(block, "type", None)
                            )
                            in _ASSISTANT_BLOCK_TYPES
                            for block in msg.raw_blocks
                        ):
                            content = msg.raw_blocks
                    if (
                        anthropic_messages
                        and anthropic_messages[-1]["role"] == "assistant"
                    ):
                        prev = anthropic_messages[-1]["content"]
                        if isinstance(prev, list) or isinstance(content, list):
                            # Can't safely merge block lists, start new turn
                            anthropic_messages.append(
                                {"role": "assistant", "content": content}
                            )
                        else:
                            anthropic_messages[-1]["content"] = prev + "\n\n" + content
                    else:
                        anthropic_messages.append(
                            {"role": "assistant", "content": content}
                        )

        if pending_tool_results:
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": pending_tool_results,
                }
            )

        return anthropic_messages

    def _convert_to_anthropic_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        generic_schema = tool.get_generic_schema()
        dict_of_schema = generic_schema.model_dump()
        parameters_dict: Dict[str, Any] = dict_of_schema.get("parameters", {})
        defs: Dict[str, Any] = parameters_dict.get("$defs", {})
        input_schema = self._clean_schema(parameters_dict, defs)
        if not isinstance(input_schema, dict):
            input_schema = {}
        input_schema.setdefault("type", "object")
        input_schema.setdefault("properties", {})
        input_schema.setdefault("required", parameters_dict.get("required", []))

        return {
            "name": generic_schema.name,
            "description": generic_schema.description,
            "input_schema": input_schema,
        }

    def _clean_schema(self, node: Any, defs: Dict[str, Any]) -> Any:
        """Preserve JSON Schema vocabulary, resolving wrappers Anthropic rejects."""
        if isinstance(node, list):
            return [self._clean_schema(item, defs) for item in node]
        if not isinstance(node, dict):
            return node

        schema_node = dict(node)
        if "$ref" in schema_node:
            schema_node = self._resolve_schema_ref(schema_node, defs)
        if "anyOf" in schema_node:
            schema_node = self._collapse_optional_any_of(schema_node, defs)

        cleaned: Dict[str, Any] = {}
        for key, value in schema_node.items():
            if key in _SCHEMA_DROP_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                cleaned[key] = {
                    prop_name: self._clean_schema(prop_schema, defs)
                    for prop_name, prop_schema in value.items()
                }
            else:
                cleaned[key] = self._clean_schema(value, defs)
        return cleaned

    def _resolve_schema_ref(
        self, schema_node: Dict[str, Any], defs: Dict[str, Any]
    ) -> Dict[str, Any]:
        def_name = schema_node["$ref"].split("/")[-1]
        resolved = dict(defs.get(def_name, {}))
        for key, value in schema_node.items():
            if key != "$ref":
                resolved[key] = value
        return resolved

    def _collapse_optional_any_of(
        self, schema_node: Dict[str, Any], defs: Dict[str, Any]
    ) -> Dict[str, Any]:
        any_of = schema_node.get("anyOf")
        if not isinstance(any_of, list):
            return schema_node

        non_null = [
            branch
            for branch in any_of
            if not (isinstance(branch, dict) and branch.get("type") == "null")
        ]
        if len(non_null) != 1 or len(non_null) == len(any_of):
            return schema_node

        branch = self._clean_schema(non_null[0], defs)
        if not isinstance(branch, dict):
            return schema_node

        collapsed = dict(branch)
        for key, value in schema_node.items():
            if key != "anyOf":
                collapsed[key] = value
        return collapsed

    async def get_max_context_tokens(self) -> Optional[int]:
        if self.model in self._context_window_cache:
            return self._context_window_cache[self.model]
        await self._populate_model_token_caches()
        return self._context_window_cache.get(self.model)

    async def get_max_output_tokens(self) -> Optional[int]:
        if self.model in self._max_output_cache:
            return self._max_output_cache[self.model]
        await self._populate_model_token_caches()
        return self._max_output_cache.get(self.model)

    async def _populate_model_token_caches(self) -> None:
        """Fetch model metadata once and cache both context and output limits."""
        window: Optional[int] = None
        max_output: Optional[int] = None
        try:
            model_info = await self.client.models.retrieve(model_id=self.model)
            # Fields `max_input_tokens` / `max_output_tokens` may not be modeled
            # by older SDK versions, but anthropic BaseModel has extra='allow'
            # so they're preserved as passthrough attributes.
            window = getattr(model_info, "max_input_tokens", None)
            max_output = getattr(model_info, "max_output_tokens", None)
        except Exception as e:
            self.logger.warning(f"Failed to retrieve model token limits: {e}")
        self._context_window_cache[self.model] = window
        self._max_output_cache[self.model] = max_output

    def validate_connection(self) -> bool:
        self.logger.info(f"Validating connection to Anthropic (model={self.model})")
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "test"}],
            )
            self.logger.info("Connection validated successfully")
            return True
        except Exception as e:
            self.logger.error(f"Connection validation failed: {e}")
            return False

    def list_models(self) -> List[str]:
        self.logger.info("Listing available Anthropic models")
        client = anthropic.Anthropic(api_key=self.api_key)
        models = client.models.list()
        model_ids = [model.id for model in models.data]
        self.logger.info(f"Found {len(model_ids)} models")
        return model_ids
