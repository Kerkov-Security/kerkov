"""Shared parsing of provider tool-call arguments."""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_EXCERPT_CHARS = 200


@dataclass(frozen=True)
class ParsedArguments:
    """Arguments ready to execute, or the reason they cannot be."""

    parameters: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def parse_tool_arguments(raw: Any) -> ParsedArguments:
    """Parse arguments without ever turning malformed input into `{}`."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ParsedArguments()
    if isinstance(raw, dict):
        return ParsedArguments(raw)
    if not isinstance(raw, str):
        return ParsedArguments(
            error=f"arguments must be a JSON object, got {type(raw).__name__}: "
            f"{_excerpt(repr(raw))}"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ParsedArguments(error=f"arguments were not valid JSON: {_excerpt(raw)}")
    if not isinstance(parsed, dict):
        return ParsedArguments(
            error=f"arguments must be a JSON object, got {_json_type(parsed)}: "
            f"{_excerpt(raw)}"
        )
    return ParsedArguments(parsed)


def _excerpt(text: str) -> str:
    text = text.strip()
    if len(text) <= _EXCERPT_CHARS:
        return text
    return text[:_EXCERPT_CHARS] + "…"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array"
