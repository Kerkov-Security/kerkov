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

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
    ) -> LLMResponse:
        """
        Generate a completion from Gemini.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0 to 2.0 for Gemini)
            max_tokens: Maximum tokens to generate
            tools: Optional list of tools the model can use

        Returns:
            LLMResponse object with the completion
        """
        gemini_messages = self._convert_messages(messages)

        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        
        gemini_tools = None
        if tools:
            tool_declarations = [self._convert_to_gemini_tool_schema(tool) for tool in tools]
            gemini_tools = [genai.types.Tool(function_declarations=tool_declarations)]

        generativeModel = genai.GenerativeModel(
            self.model,
            tools=gemini_tools if gemini_tools else None,
        )

        response = await generativeModel.generate_content_async(
            gemini_messages,
            generation_config=generation_config,
        )

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
            content = "I apologize, but I was unable to generate a proper response. Please try again."

        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            tool_calls=tool_calls,
            raw_response=response,
        )

    async def count_tokens(self, text: str) -> int:
        """
        Count tokens in the given text using Gemini's tokenizer.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        model = genai.GenerativeModel(self.model)
        result = await model.count_tokens_async(text)
        return result.total_tokens

    def validate_connection(self) -> bool:
        """
        Validate the Gemini API connection and credentials.

        Returns:
            True if connection is valid, False otherwise
        """
        try:
            # Try to list models to validate API key
            list(genai.list_models())
            return True
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """
        List available Gemini models.

        Returns:
            List of model names
        """
        models = []
        for model in genai.list_models():
            model_name = getattr(model, "name", None)
            if model_name:
                models.append(model_name)
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
                # Gemini uses function_response; keep as user-visible tool output
                tool_text = msg.content
                if msg.tool_name:
                    tool_text = f"Tool '{msg.tool_name}' result:\n{msg.content}"
                gemini_messages.append({
                    "role": "user",
                    "parts": [tool_text],
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
    
