from typing import Any, Dict, List, Optional, TYPE_CHECKING
import anthropic

from src.providers import BaseLLMProvider
from src.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall

if TYPE_CHECKING:
    from src.tools.base_tool import BaseTool

class AnthropicProvider(BaseLLMProvider):

    def __init__(self, api_key: str, model: str):
        """
        Initialize the Gemini provider.

        Args:
            api_key: Google API key
            model: Model name (default: gemini-pro)
        """
        super().__init__(api_key, model)
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
    ) -> LLMResponse:
        system_prompt = ""
        if messages[0].role == "system":
            system_prompt = messages[0].content
            messages = messages[1:]

        anthropic_messages = self._convert_messages(messages)

        anthropic_tools = [self._convert_to_anthropic_tool_schema(tool) for tool in tools]

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens or 4096,
            temperature=temperature,
            tools=anthropic_tools,
            system=system_prompt,
            messages=anthropic_messages,
        ) as stream:
            message = await stream.get_final_message()
                                                                                                                                          
        content = ""
        tool_calls = []                                                                                                                                                  
        for block in message.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    tool_name=block.name,
                    parameters=block.input,
                ))

        usage = None
        if message.usage:
            usage = TokenUsage(
                prompt_tokens=message.usage.input_tokens,
                completion_tokens=message.usage.output_tokens,
                total_tokens=message.usage.input_tokens + message.usage.output_tokens,
            )

        return LLMResponse(
            content=content,
            model=self.model,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            raw_response=message,
        )

    def _convert_messages(self, messages: List[Message]) -> List[Dict]:
        anthropic_messages = []

        user_message = ""
        assistant_message = ""

        for msg in messages:
            if msg.role in ("system", "tool", "user"):
                if assistant_message:
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": assistant_message,
                    })
                    assistant_message = ""

                user_message += ("\n\n" + msg.content) if user_message else msg.content
            elif msg.role == "assistant":
                if user_message:
                    anthropic_messages.append({
                        "role": "user",
                        "content": user_message,
                    })
                    user_message = ""
                
                assistant_message += ("\n\n" + msg.content) if assistant_message else msg.content

        if user_message:
            anthropic_messages.append({
                "role": "user",
                "content": user_message,
            })
        
        if assistant_message:
            anthropic_messages.append({
                "role": "assistant",
                "content": assistant_message,
            })

        return anthropic_messages
    
    def _convert_to_anthropic_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        generic_schema = tool.get_generic_schema()
        dict_of_schema = generic_schema.model_dump()
        parameters_dict: Dict[str, Any] = dict_of_schema.get("parameters", {})

        anthropic_tool_props = {}
        for prop_name, prop_data in parameters_dict.get("properties", {}).items():
            prop_type = self._extract_property_type(prop_data)
            cleaned_prop = {
              "type": prop_type,
              "description": prop_data.get("description", ""),
            }

            anthropic_tool_props[prop_name] = cleaned_prop

        return {
            "name": generic_schema.name,
            "description": generic_schema.description,
            "input_schema": {
                "type": "object",
                "properties": anthropic_tool_props,
                "required": parameters_dict.get("required", []),
            },
        }
    
    def _extract_property_type(self, prop_data: Dict[str, Any]) -> str:
        if "type" in prop_data:
            return prop_data["type"]
        
        if "anyOf" in prop_data:
            found_type = None

            for type_option in prop_data["anyOf"]:
                given_type = type_option.get("type")

                if given_type == "null":
                    continue
                elif given_type:
                    if found_type and found_type != given_type:
                        raise ValueError(f"anthropic doesn't support union types: {found_type} | {given_type}")
                    found_type = given_type

            if found_type:
                return found_type
        
        return "string"
    
    async def count_tokens(self, text):
        result = await self.client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
        )

        return result.input_tokens

    def validate_connection(self) -> bool:
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            client.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": "test"}],
            )
            return True
        except Exception:
            return False

    def list_models(self) -> List[str]:
        client = anthropic.Anthropic(api_key=self.api_key)
        models = client.models.list()
        return [model.id for model in models.data]
    