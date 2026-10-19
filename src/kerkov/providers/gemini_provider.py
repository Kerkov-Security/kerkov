import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from google import genai
from google.genai import types

from kerkov.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.replay import replay_tool_calls
from kerkov.providers.workers import ProviderWorkersBusy, run_provider_worker

if TYPE_CHECKING:
    from kerkov.tools.base_tool import BaseTool


class GeminiProvider(BaseLLMProvider):
    """Google Gemini LLM provider."""

    def __init__(self, api_key: str, model: str):
        """
        Initialize the Gemini provider.

        Args:
            api_key: Google API key
            model: Model name (default: gemini-pro)
        """
        super().__init__(api_key, model)
        # The google-genai Client holds its key per instance, so a detached
        # setup candidate cannot change another provider's authentication.
        self.client = genai.Client(api_key=api_key)
        self._context_window_cache: Dict[str, Optional[int]] = {}
        self._max_output_cache: Dict[str, Optional[int]] = {}
        self.logger.info(f"Initialized GeminiProvider with model '{model}'")

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """
        Generate a completion from Gemini.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0 to 2.0 for Gemini). When
                None, Gemini's own default is used.
            max_tokens: Maximum tokens to generate
            tools: Optional list of tools the model can use

        Returns:
            LLMResponse object with the completion
        """
        self.logger.info(
            f"complete() called: model={self.model}, "
            f"messages={len(messages)}, temp={temperature}, "
            f"max_tokens={max_tokens}, tools={len(tools) if tools else 0}"
        )

        gemini_messages = self._convert_messages(messages)

        config_kwargs: Dict[str, Any] = {"max_output_tokens": max_tokens}
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        if tools:
            tool_declarations = [
                self._convert_to_gemini_tool_schema(tool) for tool in tools
            ]
            config_kwargs["tools"] = [
                types.Tool(function_declarations=tool_declarations)
            ]
        generation_config = types.GenerateContentConfig(**config_kwargs)

        parts, usage = await self._generate(
            gemini_messages, generation_config, on_delta
        )

        tool_calls: Optional[List[ToolCall]] = None
        content = ""
        raw_blocks: List[Dict[str, Any]] = []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call and function_call.name:
                if tool_calls is None:
                    tool_calls = []
                # The new SDK returns call args as a native, JSON-safe dict.
                args = dict(function_call.args) if function_call.args else {}
                # Gemini doesn't return tool-call ids, but the engine needs
                # one to pair the result back to the call on the next turn.
                call_id = f"call_{uuid.uuid4().hex[:8]}"
                tool_calls.append(
                    ToolCall(
                        tool_name=function_call.name,
                        parameters=args,
                        call_id=call_id,
                    )
                )
                raw_blocks.append(
                    {
                        "type": "function_call",
                        "id": call_id,
                        "name": function_call.name,
                        "args": args,
                    }
                )
            elif getattr(part, "text", ""):
                content += part.text
                raw_blocks.append({"type": "text", "text": part.text})

        if not content and not tool_calls:
            self.logger.warning(
                "Gemini returned empty response (no content or tool calls)"
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
            # JSON-safe assistant blocks so the engine can persist them and
            # replay function_call/function_response parts on the next turn.
            raw_response={"output": raw_blocks} if raw_blocks else None,
        )

    async def _generate(
        self,
        gemini_messages: List[Any],
        generation_config: Any,
        on_delta: Optional[Callable[[str], None]],
    ) -> "tuple[List[Any], Optional[TokenUsage]]":
        """Run one Gemini call, streaming text deltas when a callback is given.

        Returns the flat list of response parts (text and function_call) and the
        final usage, so the caller's single parse path serves both modes.
        """
        parts: List[Any] = []
        usage: Optional[TokenUsage] = None
        try:
            if on_delta is not None:
                stream = await self.client.aio.models.generate_content_stream(
                    model=self.model,
                    contents=gemini_messages,
                    config=generation_config,
                )
                async for chunk in stream:
                    usage = self._usage_from(chunk) or usage
                    for part in self._parts_of(chunk):
                        parts.append(part)
                        text = getattr(part, "text", "")
                        if text:
                            on_delta(text)
            else:
                response = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=gemini_messages,
                    config=generation_config,
                )
                usage = self._usage_from(response)
                parts = list(self._parts_of(response))
        except asyncio.CancelledError:
            self.logger.info("Gemini request cancelled by user")
            raise
        except Exception as e:
            self.logger.error(f"Gemini API call failed: {e}")
            raise
        return parts, usage

    @staticmethod
    def _usage_from(response: Any) -> Optional[TokenUsage]:
        meta = getattr(response, "usage_metadata", None)
        if not meta:
            return None
        prompt = meta.prompt_token_count or 0
        candidates = meta.candidates_token_count or 0
        total = meta.total_token_count or 0
        thoughts = getattr(meta, "thoughts_token_count", None)
        if thoughts is None:
            # Without a thoughts count, Gemini still bills total as
            # prompt + thoughts + candidates; everything generated is output.
            completion_tokens = max(candidates, total - prompt)
        else:
            completion_tokens = candidates + thoughts
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion_tokens,
            total_tokens=total,
            cache_read_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
        )

    @staticmethod
    def _parts_of(response: Any) -> List[Any]:
        if not response.candidates:
            return []
        content = response.candidates[0].content
        if content and content.parts:
            return list(content.parts)
        return []

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
            model_name = f"models/{self.model.removeprefix('models/')}"
            # The blocking lookup runs in the bounded provider worker pool so a
            # canceled request cannot leak a thread or exceed capacity.
            model_info = await run_provider_worker(
                self.client.models.get, model=model_name
            )
            window = getattr(model_info, "input_token_limit", None)
            max_output = getattr(model_info, "output_token_limit", None)
        except ProviderWorkersBusy:
            # A capacity limit is temporary, not unknown model metadata. Let
            # selection fail safely without caching fallback context limits.
            raise
        except Exception as e:
            self.logger.warning(f"Failed to retrieve model token limits: {e}")
        self._context_window_cache[self.model] = window
        self._max_output_cache[self.model] = max_output

    def validate_connection(self) -> bool:
        """
        Validate the Gemini API connection and credentials.

        Returns:
            True if connection is valid, False otherwise
        """
        self.logger.info(f"Validating connection to Gemini (model={self.model})")
        try:
            list(self.client.models.list())
            self.logger.info("Connection validated successfully")
            return True
        except Exception as e:
            self.logger.error(f"Connection validation failed: {e}")
            return False

    def list_models(self) -> List[str]:
        """
        List Gemini models supporting the generateContent chat endpoint.

        Returns:
            List of model names
        """
        self.logger.info("Listing available Gemini models")
        models = []
        for model in self.client.models.list():
            model_name = getattr(model, "name", None)
            actions = getattr(model, "supported_actions", None) or []
            # The catalog also contains embeddings and legacy generation APIs
            # that cannot serve this provider's generate_content calls.
            if model_name and "generateContent" in actions:
                models.append(model_name)
        self.logger.info(f"Found {len(models)} models")
        return models

    def _convert_messages(self, messages: List[Message]) -> List[Any]:
        """
        Convert standard messages to Gemini format.

        Gemini uses 'user' and 'model' roles, not 'assistant'. System messages
        are folded into the next user turn. Assistant tool calls and their
        results are rebuilt as function_call / function_response parts so the
        model sees its own prior actions. Without this, tool turns collapse into
        empty 'model' turns and the model re-issues the same call forever.

        Returns a list of Gemini Content objects for generate_content.
        """
        gemini_messages: List[Any] = []
        system_content = ""
        call_id_to_name: Dict[str, str] = {}
        pending_tool_parts: List[Any] = []

        def flush_tool_parts() -> None:
            if pending_tool_parts:
                gemini_messages.append(
                    types.Content(role="user", parts=list(pending_tool_parts))
                )
                pending_tool_parts.clear()

        for msg in messages:
            if msg.role != "tool":
                flush_tool_parts()

            if msg.role == "system":
                system_content += msg.content + "\n"
            elif msg.role == "user":
                content = msg.content
                if system_content:
                    content = system_content + content
                    system_content = ""
                gemini_messages.append(
                    types.Content(role="user", parts=[types.Part(text=content)])
                )
            elif msg.role == "assistant":
                if system_content:
                    gemini_messages.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=system_content.strip())],
                        )
                    )
                    system_content = ""
                gemini_messages.append(
                    types.Content(
                        role="model",
                        parts=self._assistant_parts(msg, call_id_to_name),
                    )
                )
            elif msg.role == "tool":
                pending_tool_parts.append(self._tool_part(msg, call_id_to_name))

        flush_tool_parts()

        # Safety check: leftover system content with no following turn.
        if system_content:
            gemini_messages.append(
                types.Content(
                    role="user", parts=[types.Part(text=system_content.strip())]
                )
            )

        return gemini_messages

    def _assistant_parts(
        self, msg: Message, call_id_to_name: Dict[str, str]
    ) -> List[Any]:
        """Rebuild an assistant turn's parts, including any function_call parts."""
        parts: List[Any] = [types.Part(text=msg.content)] if msg.content else []
        for call in replay_tool_calls(msg.raw_blocks):
            call_id_to_name[call.call_id] = call.name
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        name=call.name,
                        args=call.arguments_dict(),
                    )
                )
            )

        return parts or [types.Part(text=msg.content or " ")]

    def _tool_part(self, msg: Message, call_id_to_name: Dict[str, str]) -> Any:
        """Rebuild a tool result as a function_response part (text on fallback)."""
        name = call_id_to_name.get(msg.call_id or "")
        if name:
            return types.Part(
                function_response=types.FunctionResponse(
                    name=name,
                    response={"result": msg.content},
                )
            )
        # No matching call (legacy data or a skipped call). The API rejects a
        # function_response with an unknown name, so surface it as plain text.
        return types.Part(text=f"[tool result] {msg.content}")

    def _convert_to_gemini_tool_schema(
        self, tool: "BaseTool"
    ) -> types.FunctionDeclaration:
        generic_schema = tool.get_generic_schema()
        dict_of_schema = generic_schema.model_dump()
        parameters_dict: Dict[str, Any] = dict_of_schema.get("parameters", {})
        defs: Dict[str, Any] = parameters_dict.get("$defs", {})

        gemini_tool_props = {}
        for prop_name, prop_data in parameters_dict.get("properties", {}).items():
            gemini_tool_props[prop_name] = self._clean_schema(prop_data, defs)

        parameters = types.Schema.model_validate(
            {
                "type": "object",
                "properties": gemini_tool_props,
                "required": parameters_dict.get("required", []),
            }
        )
        return types.FunctionDeclaration(
            name=generic_schema.name,
            description=generic_schema.description,
            parameters=parameters,
        )

    def _clean_schema(
        self, prop_data: Dict[str, Any], defs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Recursively convert a JSON Schema node to a Gemini-compatible Schema dict."""
        description = prop_data.get("description", "")

        # Resolve $ref first, preserving any sibling fields (e.g. description)
        if "$ref" in prop_data:
            def_name = prop_data["$ref"].split("/")[-1]
            resolved = dict(defs.get(def_name, {}))
            for k, v in prop_data.items():
                if k != "$ref":
                    resolved[k] = v
            prop_data = resolved

        result: Dict[str, Any] = {
            "description": description or prop_data.get("description", "")
        }

        # Unwrap anyOf into a single type + nullable
        if "anyOf" in prop_data and "type" not in prop_data:
            non_null = [o for o in prop_data["anyOf"] if o.get("type") != "null"]
            is_nullable = len(non_null) < len(prop_data["anyOf"])
            if len(non_null) > 1:
                raise ValueError(
                    f"Unsupported union schema (multiple non-null branches): {prop_data['anyOf']}"
                )
            if non_null:
                branch = dict(non_null[0])
                # Resolve $ref inside anyOf branch (e.g. Pydantic Optional[NestedModel])
                if "$ref" in branch:
                    def_name = branch["$ref"].split("/")[-1]
                    resolved = dict(defs.get(def_name, {}))
                    for k, v in branch.items():
                        if k != "$ref":
                            resolved[k] = v
                    branch = resolved
                prop_data = branch
                if is_nullable:
                    result["nullable"] = True

        prop_type = prop_data.get("type", "string")
        result["type"] = prop_type

        if "enum" in prop_data:
            result["format"] = "enum"
            result["enum"] = prop_data["enum"]

        if prop_type == "array" and "items" in prop_data:
            result["items"] = self._clean_schema(prop_data["items"], defs)

        if prop_type == "object" and "properties" in prop_data:
            result["properties"] = {
                k: self._clean_schema(v, defs)
                for k, v in prop_data["properties"].items()
            }
            if "required" in prop_data:
                result["required"] = prop_data["required"]

        return result
