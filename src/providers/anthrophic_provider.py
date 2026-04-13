import asyncio
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
        self.logger.info(f"Initialized AnthropicProvider with model '{model}'")

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
    ) -> LLMResponse:
        self.logger.info(
            f"complete() called: model={self.model}, "
            f"messages={len(messages)}, temp={temperature}, "
            f"max_tokens={max_tokens}, tools={len(tools) if tools else 0}"
        )

        system_prompt = ""
        if messages[0].role == "system":
            system_prompt = messages[0].content
            messages = messages[1:]

        anthropic_messages = self._convert_messages(messages)

        anthropic_tools = [self._convert_to_anthropic_tool_schema(tool) for tool in tools] if tools else []

        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens or 4096,
                temperature=temperature,
                tools=anthropic_tools,
                system=system_prompt,
                messages=anthropic_messages,
            ) as stream:
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
                tool_calls.append(ToolCall(
                    tool_name=block.name,
                    parameters=block.input,
                    call_id=block.id,
                ))

        usage = None
        if message.usage:
            usage = TokenUsage(
                prompt_tokens=message.usage.input_tokens,
                completion_tokens=message.usage.output_tokens,
                total_tokens=message.usage.input_tokens + message.usage.output_tokens,
            )

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

    def _convert_messages(self, messages: List[Message]) -> List[Dict]:
        anthropic_messages = []
        pending_tool_results: List[Dict] = []

        for msg in messages:
            if msg.role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.call_id,
                    "content": msg.content,
                })
            else:
                if pending_tool_results:
                    anthropic_messages.append({
                        "role": "user",
                        "content": pending_tool_results,
                    })
                    pending_tool_results = []

                if msg.role in ("user", "system"):
                    if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                        prev = anthropic_messages[-1]["content"]
                        if isinstance(prev, str):
                            anthropic_messages[-1]["content"] = prev + "\n\n" + msg.content
                        else:
                            # prev is a list (tool_results block), start a new user turn
                            anthropic_messages.append({"role": "user", "content": msg.content})
                    else:
                        anthropic_messages.append({"role": "user", "content": msg.content})
                elif msg.role == "assistant":
                    content = msg.raw_blocks if msg.raw_blocks is not None else msg.content
                    if anthropic_messages and anthropic_messages[-1]["role"] == "assistant":
                        prev = anthropic_messages[-1]["content"]
                        if isinstance(prev, list) or isinstance(content, list):
                            # Can't safely merge block lists, start new turn
                            anthropic_messages.append({"role": "assistant", "content": content})
                        else:
                            anthropic_messages[-1]["content"] = prev + "\n\n" + content
                    else:
                        anthropic_messages.append({"role": "assistant", "content": content})

        if pending_tool_results:
            anthropic_messages.append({
                "role": "user",
                "content": pending_tool_results,
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
    
    def get_max_context_tokens(self) -> Optional[int]:
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            model_info = client.models.retrieve(model_id=self.model)
            return model_info.context_window
        except Exception as e:
            self.logger.warning(f"Failed to retrieve model context window: {e}")
            return None

    async def count_tokens(self, text):
        result = await self.client.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
        )

        return result.input_tokens

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
    