"""Embedding hook for semantic recall.

Semantic recall is opt-in (`KB_EMBEDDINGS`, default off). With no backend the
knowledge base is pure FTS5 keyword search, which needs nothing beyond the
stdlib `sqlite3` and works fully offline. This module defines only the seam; no
concrete remote backend ships yet.
"""

import math
import struct
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

# Bodies are chunked before embedding so a hit points at the relevant passage
# of a large scan rather than the whole blob.
DEFAULT_CHUNK_CHARS = 2000


class EmbeddingBackend(ABC):
    """Turns text into vectors. Implementations may call out to a provider."""

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch of texts, returning one vector per input."""
        pass


def pack_vector(vector: Sequence[float]) -> bytes:
    """Serialize a vector for BLOB storage (float32, little-endian)."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> List[float]:
    """Inverse of `pack_vector`."""
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, 0.0 when either side has no magnitude."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = float(sum(x * y for x, y in zip(a, b)))
    norm_a = math.sqrt(float(sum(x * x for x in a)))
    norm_b = math.sqrt(float(sum(y * y for y in b)))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def chunk_text(text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> List[str]:
    """Split text into fixed-size chunks, preferring a line boundary.

    Scan output is line-oriented, so breaking mid-line would split a host or a
    port off its finding. Falls back to a hard cut when a single line exceeds
    the chunk size.
    """
    if not text:
        return []
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_chars
        if end >= len(text):
            chunks.append(text[start:])
            break
        boundary = text.rfind("\n", start, end)
        if boundary <= start:
            boundary = end
        chunks.append(text[start:boundary])
        start = boundary
    return [c for c in chunks if c.strip()]


def resolve_embedding_backend(name: Optional[str]) -> Optional[EmbeddingBackend]:
    """Look up a backend by config name.

    No concrete backend ships yet, so this always returns None. It exists so
    the wiring is in place and `KB_EMBEDDING_PROVIDER` has a single resolution
    point when one lands.
    """
    return None
