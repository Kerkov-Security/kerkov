"""Data models for the engagement knowledge base."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

# Artifact kinds. `tool_output` is what the ingestion hook writes; the rest are
# available for callers that classify more precisely (a file the agent read, a
# credential it harvested, a note it chose to record).
KIND_TOOL_OUTPUT = "tool_output"
KIND_FILE = "file"
KIND_CREDENTIAL = "credential"
KIND_NOTE = "note"
KIND_HTTP_RESPONSE = "http_response"


class Artifact(BaseModel):
    """A stored artifact, as written."""

    engagement_id: str
    kind: str = KIND_TOOL_OUTPUT
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
    kind: str
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
