from pydantic import BaseModel
from typing import Any, Dict, List, Optional


class TokenUsage(BaseModel):
    """Token usage tracking."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ToolCall(BaseModel):
    """Represents a tool call request from the LLM."""

    tool_name: str
    parameters: Dict[str, Any]
    call_id: Optional[str] = None  # For tracking multiple tool calls


class LLMResponse(BaseModel):
    """Standard response from LLM providers."""

    content: str
    model: str
    usage: Optional[TokenUsage] = None
    tool_calls: Optional[List[ToolCall]] = None
    raw_response: Optional[Any] = None


class Message(BaseModel):
    """Standard message format for conversations."""

    role: str  # "user", "assistant", "system", "tool"
    content: str
    call_id: Optional[str] = None  # For tool result messages, references the ToolCall.call_id
    raw_blocks: Optional[Any] = None  # Provider-specific raw content blocks (e.g. tool_use blocks)
