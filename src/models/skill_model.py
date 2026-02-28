from pydantic import BaseModel
from typing import List, Optional


class Skill(BaseModel):
    """Represents a loaded kerkov skill."""

    name: str
    description: str
    tags: List[str] = []
    tools: Optional[List[str]] = None  # None = all tools available
    content: str  # The skill prompt injected into context
    source_path: str
    is_builtin: bool = False
