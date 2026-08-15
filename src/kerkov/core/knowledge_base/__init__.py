"""Engagement knowledge base: FTS5 store + recall over what the agent gathered."""

from kerkov.core.knowledge_base.embeddings import (
    EmbeddingBackend,
    resolve_embedding_backend,
)
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    build_match_query,
    delete_kb_for_root,
    kb_path_for,
    open_knowledge_base,
    resolve_root_engagement_id,
)
from kerkov.core.knowledge_base.models import (
    KIND_CREDENTIAL,
    KIND_FILE,
    KIND_HTTP_RESPONSE,
    KIND_NOTE,
    KIND_TOOL_OUTPUT,
    Artifact,
    ArtifactHit,
)

__all__ = [
    "Artifact",
    "ArtifactHit",
    "EmbeddingBackend",
    "EngagementKnowledgeBase",
    "KIND_CREDENTIAL",
    "KIND_FILE",
    "KIND_HTTP_RESPONSE",
    "KIND_NOTE",
    "KIND_TOOL_OUTPUT",
    "build_match_query",
    "delete_kb_for_root",
    "kb_path_for",
    "open_knowledge_base",
    "resolve_embedding_backend",
    "resolve_root_engagement_id",
]
