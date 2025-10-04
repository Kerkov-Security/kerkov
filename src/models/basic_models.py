from pydantic import BaseModel
from typing import Any, Optional

class TokenUsage(BaseModel):
    """Token usage tracking."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResponse(BaseModel):
    """Standard response from LLM providers."""

    content: str
    model: str
    usage: Optional[TokenUsage] = None
    raw_response: Optional[Any] = None


class Message(BaseModel):
    """Standard message format for conversations."""

    role: str  # "user", "assistant", "system"
    content: str
