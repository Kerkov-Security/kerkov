"""
Pydantic models for the skills framework.
"""

from typing import Optional

from pydantic import BaseModel, field_validator


class RequiresSpec(BaseModel):
    """Binary requirements for a skill."""

    bins: Optional[list[str]] = None


class KerkovMetadata(BaseModel):
    """Security-domain metadata under metadata.kerkov."""

    category: Optional[str] = None
    tactic: Optional[str] = None
    mitre: Optional[list[str]] = None
    difficulty: Optional[str] = None
    platforms: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    products: Optional[list[str]] = None
    vuln_classes: Optional[list[str]] = None
    cves: Optional[list[str]] = None
    lab_sources: Optional[list[str]] = None
    safe_verification: Optional[bool] = None
    requires: Optional[RequiresSpec] = None

    @field_validator("mitre", mode="before")
    @classmethod
    def _normalize_mitre(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        return value


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
