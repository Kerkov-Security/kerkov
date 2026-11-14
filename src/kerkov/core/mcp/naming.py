"""Map MCP tool names onto names every supported provider accepts.

A mapped name that was sanitized, truncated or suffixed cannot be parsed back, so
callers keep the returned table from mapped name to original name and never split
the string.
"""

from __future__ import annotations

import hashlib
import re
from typing import AbstractSet, Dict, Iterable

PREFIX = "mcp__"
SEPARATOR = "__"
# Strictest supported provider limit (OpenAI-compatible function names and Gemini).
MAX_TOOL_NAME_LENGTH = 64
_HASH_LENGTH = 8
_INVALID = re.compile(r"[^A-Za-z0-9_-]")


def map_tool_names(
    server_key: str, tool_names: Iterable[str], taken: AbstractSet[str]
) -> Dict[str, str]:
    """Return `{mapped: original}` for one server, avoiding every name in `taken`."""
    mapped: Dict[str, str] = {}
    for original in sorted(set(tool_names)):
        candidate = _candidate(server_key, original)
        if candidate in taken or candidate in mapped:
            candidate = _with_suffix(f"{PREFIX}{server_key}{SEPARATOR}", original, 0)
        attempt = 1
        while candidate in taken or candidate in mapped:
            candidate = _with_suffix(
                f"{PREFIX}{server_key}{SEPARATOR}", original, attempt
            )
            attempt += 1
        mapped[candidate] = original
    return mapped


def _candidate(server_key: str, original: str) -> str:
    prefix = f"{PREFIX}{server_key}{SEPARATOR}"
    sanitized = _INVALID.sub("_", original)
    name = prefix + sanitized
    if sanitized == original and original and len(name) <= MAX_TOOL_NAME_LENGTH:
        return name
    return _with_suffix(prefix, original, 0)


def _with_suffix(prefix: str, original: str, attempt: int) -> str:
    """Bounded, deterministic name: sanitized stem plus a hash of the original."""
    digest = hashlib.sha256(f"{original}\0{attempt}".encode()).hexdigest()
    suffix = "_" + digest[:_HASH_LENGTH]
    room = MAX_TOOL_NAME_LENGTH - len(prefix) - len(suffix)
    stem = _INVALID.sub("_", original)[: max(room, 0)]
    return prefix + stem + suffix
