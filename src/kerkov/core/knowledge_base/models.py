"""Data models for the engagement knowledge base."""

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ArtifactKind(str, Enum):
    """What an artifact holds — the `recall` kind filter."""

    TOOL_OUTPUT = "tool_output"
    FILE = "file"
    HTTP_RESPONSE = "http_response"


class LedgerSection(str, Enum):
    """A section of the findings ledger — the model's structured working memory."""

    HOSTS = "hosts"
    SERVICES = "services"
    CREDENTIALS = "credentials"
    FINDINGS = "findings"
    FLAGS = "flags"
    NEXT_STEPS = "next_steps"


class Artifact(BaseModel):
    """A stored artifact, as written."""

    engagement_id: str
    kind: ArtifactKind = ArtifactKind.TOOL_OUTPUT
    source: str = ""
    title: str = ""
    body: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Denormalized from the engagement that produced this row so a tree-wide
    # search can label a hit ("recon agent") without re-reading engagement
    # metadata off disk for every result.
    agent_type: Optional[str] = None


class ArtifactHit(BaseModel):
    """One search result: a stored artifact plus its match context."""

    artifact_id: int
    engagement_id: str
    kind: ArtifactKind
    source: str
    title: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    agent_type: Optional[str] = None
    created_at: str = ""
    size_bytes: int = 0
    # FTS5 snippet around the match, or the head of the body for a direct
    # `artifact#N` fetch. `body` is populated only when the caller asked for
    # the full record.
    snippet: str = ""
    body: Optional[str] = None


class LedgerEntry(BaseModel):
    """One confirmed fact in the findings ledger, as stored."""

    id: Optional[int] = None
    engagement_id: str
    section: LedgerSection
    entry: Dict[str, Any] = Field(default_factory=dict)
    artifact_ref: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""
