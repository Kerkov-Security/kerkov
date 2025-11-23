"""
Google Gemini provider implementation.
"""

from typing import Any, Dict, List, Optional, TYPE_CHECKING

import google.generativeai as genai

from src.models.basic_models import LLMResponse, Message, TokenUsage, ToolCall
from src.providers.base_provider import BaseLLMProvider

if TYPE_CHECKING:
    from src.tools.base_tool import BaseTool

class GeminiProvider(BaseLLMProvider):
    """Google Gemini LLM provider."""

    def __init__(self, api_key: str, model: str, **kwargs):
        """
        Initialize the Gemini provider.

        Args:
            api_key: Google API key
            model: Model name (default: gemini-pro)
            **kwargs: Additional configuration
        """
        super().__init__(api_key, model, **kwargs)
        genai.configure(api_key=api_key)

    async def complete(
        self,
        messages: List[Message],
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tools: Optional[List["BaseTool"]] = None,
        **kwargs,
    ) -> LLMResponse:
        """
        Generate a completion from Gemini.

        Args:
            messages: List of conversation messages
            temperature: Sampling temperature (0.0 to 2.0 for Gemini)
            max_tokens: Maximum tokens to generate
            tools: Optional list of tools the model can use
            **kwargs: Additional Gemini-specific parameters

        Returns:
            LLMResponse object with the completion
        """
        # Convert messages to Gemini format
        gemini_messages = self._convert_messages(messages)

        # Configure generation parameters
        generation_config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            **kwargs,
        )

        # Convert tools to Gemini format if provided
        gemini_tools = None
        if tools:
            tool_declarations = [self._create_tool_schema(tool) for tool in tools]
            gemini_tools = [genai.types.Tool(function_declarations=tool_declarations)]

        # Create model with or without tools
        generativeModel = genai.GenerativeModel(
            self.model,
            tools=gemini_tools if gemini_tools else None,
        )

        response = await generativeModel.generate_content_async(
            gemini_messages,
            generation_config=generation_config,
        )

        # Extract token usage if available
        usage = None
        if hasattr(response, 'usage_metadata'):
            usage = TokenUsage(
                prompt_tokens=response.usage_metadata.prompt_token_count,
                completion_tokens=response.usage_metadata.candidates_token_count,
                total_tokens=response.usage_metadata.total_token_count,
            )

        # Check for function calls in response
        tool_calls = None
        content = ""

        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                # Check if this part is a function call
                if hasattr(part, 'function_call') and part.function_call:
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append(ToolCall(
                        tool_name=part.function_call.name,
                        parameters=dict(part.function_call.args),
                    ))
                # Otherwise it's text content
                elif hasattr(part, 'text'):
                    content += part.text

        # If no content extracted and no tool calls, provide a fallback
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

        # Safety check: if there's leftover system content, add it as a final user message
        if system_content:
            gemini_messages.append({
                "role": "user",
                "parts": [system_content.strip()],
            })

        return gemini_messages

    def _create_tool_schema(self, tool: "BaseTool") -> Dict[str, Any]:
        """
        Create Gemini function declaration from BaseTool.

        Args:
            tool: Tool to convert

        Returns:
            Gemini function declaration dictionary
        """
        # Define schemas for all supported tools
        schemas = {
            "shell": {
                "name": "shell",
                "description": "Execute quick, one-off shell commands that don't require user input (bash on Linux/Mac, cmd/powershell on Windows). Use for simple commands like listing files, checking system info, or running scripts. For interactive sessions or long-running commands, use persistent_shell instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute"
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Command timeout in seconds (optional)"
                        }
                    },
                    "required": ["command"]
                }
            },
            "write": {
                "name": "write",
                "description": "Write content to a file. Can create new files or overwrite existing ones. Use 'append' parameter to append to existing files instead of overwriting.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file to write"
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file"
                        },
                        "append": {
                            "type": "boolean",
                            "description": "If true, append to file instead of overwriting (default: false)"
                        }
                    },
                    "required": ["file_path", "content"]
                }
            },
            "read": {
                "name": "read",
                "description": "Read content from a file. Returns the file contents as text. Has a maximum file size limit of 10MB for safety.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to the file to read"
                        },
                        "max_size": {
                            "type": "integer",
                            "description": "Maximum file size to read in bytes (optional, default: 10MB)"
                        }
                    },
                    "required": ["file_path"]
                }
            },
            "fetch": {
                "name": "fetch",
                "description": "Fetch content from a URL using HTTP/HTTPS. Supports GET and POST requests. Returns the response body as text. Has a maximum response size limit of 10MB for safety.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to fetch (must start with http:// or https://)"
                        },
                        "method": {
                            "type": "string",
                            "description": "HTTP method (GET or POST, default: GET)",
                            "enum": ["GET", "POST"]
                        },
                        "headers": {
                            "type": "object",
                            "description": "Optional HTTP headers as key-value pairs"
                        },
                        "data": {
                            "type": "string",
                            "description": "Optional request body for POST requests"
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Request timeout in seconds (optional)"
                        }
                    },
                    "required": ["url"]
                }
            },
            "persistent_shell": {
                "name": "persistent_shell",
                "description": "Execute commands in a persistent shell session that maintains state across commands. Use for interactive tools (msfconsole, ssh), long-running scans, or when the user needs to provide input. Tracks user activity and supports fire-and-forget for background commands. For quick, non-interactive commands, use shell instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The command to execute in the persistent shell"
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Maximum seconds to wait for output (default: 5.0)"
                        }
                    },
                    "required": ["command"]
                }
            }
        }

        # Return the schema for the requested tool
        return schemas.get(tool.name, {})
