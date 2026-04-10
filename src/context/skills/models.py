"""
Pydantic models for the skills framework.
"""

from typing import Optional

from pydantic import BaseModel


class RequiresSpec(BaseModel):
    """Binary requirements for a skill."""

    bins: Optional[list[str]] = None


class KerkovMetadata(BaseModel):
    """Security-domain metadata under metadata.kerkov."""

    category: Optional[str] = None
    mitre: Optional[str] = None
    difficulty: Optional[str] = None
    platforms: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    requires: Optional[RequiresSpec] = None


class SkillEntry(BaseModel):
    """A parsed skill from a SKILL.md file."""

    name: str
    description: str
    user_invocable: bool = True
    disable_model_invocation: bool = False
    kerkov: Optional[KerkovMetadata] = None
    file_path: str
    content: str


class EligibilityResult(BaseModel):
    """Result of checking whether a skill's required binaries are available."""

    eligible: bool
    missing_bins: list[str] = []
