import asyncio
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import google.generativeai as genai

from src.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from src.providers.base_provider import BaseLLMProvider

if TYPE_CHECKING:
    from src.tools.base_tool import BaseTool

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
        genai.configure(api_key=api_key)
        self._context_window_cache: Dict[str, Optional[int]] = {}
        self._max_output_cache: Dict[str, Optional[int]] = {}
        self.logger.info(f"Initialized GeminiProvider with model '{model}'")

    async def complete(
        self,
        messages: List[Message],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
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

        gen_config_kwargs: Dict[str, Any] = {"max_output_tokens": max_tokens}
        if temperature is not None:
            gen_config_kwargs["temperature"] = temperature
        generation_config = genai.types.GenerationConfig(**gen_config_kwargs)

        gemini_tools = None
        if tools:
            tool_declarations = [self._convert_to_gemini_tool_schema(tool) for tool in tools]
            gemini_tools = [genai.types.Tool(function_declarations=tool_declarations)]

        generativeModel = genai.GenerativeModel(
            self.model,
            tools=gemini_tools if gemini_tools else None,
        )

        try:
            response = await generativeModel.generate_content_async(
                gemini_messages,
                generation_config=generation_config,
            )
        except asyncio.CancelledError:
            self.logger.info("Gemini request cancelled by user")
            raise
        except Exception as e:
            self.logger.error(f"Gemini API call failed: {e}")
            raise

        usage = None
        if hasattr(response, 'usage_metadata'):
            usage = TokenUsage(
                prompt_tokens=response.usage_metadata.prompt_token_count,
                completion_tokens=response.usage_metadata.candidates_token_count,
                total_tokens=response.usage_metadata.total_token_count,
            )

        tool_calls = None
        content = ""
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'function_call') and part.function_call:
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append(
                        ToolCall(
                            tool_name=part.function_call.name,
                            parameters=dict(part.function_call.args) if part.function_call.args else {},
                        )
                    )
                elif hasattr(part, 'text'):
                    content += part.text

        if not content and not tool_calls:
            self.logger.warning("Gemini returned empty response (no content or tool calls)")
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
            raw_response=response,
        )

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
            model_info = await asyncio.to_thread(genai.get_model, f"models/{self.model}")
            window = getattr(model_info, "input_token_limit", None)
            max_output = getattr(model_info, "output_token_limit", None)
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
            list(genai.list_models())
            self.logger.info("Connection validated successfully")
            return True
        except Exception as e:
            self.logger.error(f"Connection validation failed: {e}")
            return False

    def list_models(self) -> List[str]:
        """
        List available Gemini models.

        Returns:
            List of model names
        """
        self.logger.info("Listing available Gemini models")
        models = []
        for model in genai.list_models():
            model_name = getattr(model, "name", None)
            if model_name:
                models.append(model_name)
        self.logger.info(f"Found {len(models)} models")
        return models

    def _convert_messages(self, messages: List[Message]) -> List[dict]:
        """
        Convert standard messages to Gemini format.

        Gemini uses 'user' and 'model' roles, not 'assistant'.
        System messages are prepended to the first user message.

        Args:
            messages: List of standard Message objects

        Returns:
            List of Gemini-formatted message dictionaries
        """
        gemini_messages = []
        system_content = ""

        for msg in messages:
            if msg.role == "system":
                # Accumulate system messages
                system_content += msg.content + "\n"
            elif msg.role == "user":
                content = msg.content
                # Prepend system content to first user message
                if system_content:
                    content = system_content + content
                    system_content = ""
                gemini_messages.append({
                    "role": "user",
                    "parts": [content],
                })
            elif msg.role == "assistant":
                # If there's accumulated system content, add it as a user message first
                if system_content:
                    gemini_messages.append({
                        "role": "user",
                        "parts": [system_content.strip()]
                    })
                    system_content = ""

                # Gemini uses 'model' instead of 'assistant'
                gemini_messages.append({
                    "role": "model",
                    "parts": [msg.content],
                })
            elif msg.role == "tool":
                gemini_messages.append({
                    "role": "user",
                    "parts": [msg.content],
                })

        # Safety check: if there's leftover system content, add it as a final user message
        if system_content:
            gemini_messages.append(
                {
                    "role": "user",
                    "parts": [system_content.strip()],
                }
            )

        return gemini_messages
    
    def _convert_to_gemini_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        generic_schema = tool.get_generic_schema()
        dict_of_schema = generic_schema.model_dump()
        parameters_dict: Dict[str, Any] = dict_of_schema.get("parameters", {})

        gemini_tool_props = {}
        for prop_name, prop_data in parameters_dict.get("properties", {}).items():
            (prop_type, is_nullable) = self._extract_property_type(prop_data)
            cleaned_prop = {
              "type": prop_type,
              "description": prop_data.get("description", ""),
            }
            if is_nullable:
                cleaned_prop["nullable"] = True  # Only add when needed

            gemini_tool_props[prop_name] = cleaned_prop

        return {
            "name": generic_schema.name,
            "description": generic_schema.description,
            "parameters": {
                "type": "object",
                "properties": gemini_tool_props,
                "required": parameters_dict.get("required", []),
            },
        }
    
    def _extract_property_type(self, prop_data: Dict[str, Any]) -> Tuple[str, bool]:
        # Returns a tuple where: (<TYPE_OF_PROP>, <IS_NULLABLE>)
        # Only one type is allowed and it can be nullable
        if "type" in prop_data:
            return (prop_data["type"], False)
        
        if "anyOf" in prop_data:
            found_type = None
            is_nullable = False

            for type_option in prop_data["anyOf"]:
                given_type = type_option.get("type")

                if given_type == "null":
                    is_nullable = True
                elif given_type:
                    if found_type and found_type != given_type:
                        raise ValueError(f"Gemini doesn't support union types: {found_type} | {given_type}")
                    found_type = given_type

            if found_type:
                return (found_type, is_nullable) 
        
        return ("string", False) # Fallback
    
