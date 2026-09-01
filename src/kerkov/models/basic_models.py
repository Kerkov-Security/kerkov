from typing import Any, Dict, List, Optional

from pydantic import BaseModel


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
    call_id: Optional[str] = (
        None  # For tool result messages, references the ToolCall.call_id
    )
    raw_blocks: Optional[Any] = (
        None  # Provider-specific raw content blocks (e.g. tool_use blocks)
    )
    # Compaction provenance. `summary_level` marks a message the compactor
    # produced (1 = summary of raw history, 2 = rolled-up summary) so it is
    # never fed back in as fresh narrative; `artifact_ref` is the knowledge-base
    # artifact behind a role="tool" message, which lets compaction evict the
    # body and keep the handle. Both persist, so the marks survive /resume.
    summary_level: Optional[int] = None
    artifact_ref: Optional[int] = None
