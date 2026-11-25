"""One-line summaries of tool calls and results for terminal output.

The approval box shows a call in full; everywhere else the operator gets a line
bounded to the terminal width, so a long argument or result cannot flood it.
"""

import json
from typing import Any, Dict, List

from kerkov.core.session.approval_rules import (
    escape_display_controls,
    render_call_string,
)

MAX_VALUE_CHARS = 60
PREVIEW_MAX_LINES = 3
_ELLIPSIS = "…"


def summarize_call(tool_name: str, params: Dict[str, Any], width: int) -> str:
    """Render `tool(key=value, …)` on one line no wider than `width`."""
    if tool_name == "write":
        return _clip(render_call_string(tool_name, params), width)
    pieces = [
        f"{escape_display_controls(str(key))}={_format_value(value)}"
        for key, value in params.items()
    ]
    return _fit(f"{escape_display_controls(tool_name)}(", pieces, ")", width)


def summarize_output(output: str, width: int) -> List[str]:
    """Return at most a few lines previewing `output`, each within `width`."""
    if not output.strip():
        return []
    structured = _summarize_json(output, width)
    if structured is not None:
        return [structured]
    lines = output.strip("\n").splitlines()
    preview = [
        _clip(escape_display_controls(line.expandtabs(4)), width)
        for line in lines[:PREVIEW_MAX_LINES]
    ]
    hidden = len(lines) - len(preview)
    if hidden:
        preview.append(f"{_ELLIPSIS} +{hidden} more line{'s' if hidden != 1 else ''}")
    return preview


def _summarize_json(output: str, width: int) -> Any:
    stripped = output.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    if isinstance(value, dict):
        pieces = [
            f"{escape_display_controls(str(key))}: {_format_value(item)}"
            for key, item in value.items()
        ]
        return _fit("{", pieces, "}", width)
    if isinstance(value, list):
        return _format_value(value)
    return None


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            value = value[:MAX_VALUE_CHARS] + _ELLIPSIS
        # repr escapes newlines and non-printable (including bidi) characters.
        return repr(value)
    if isinstance(value, (list, tuple)):
        return _count(len(value), "item", "[", "]")
    if isinstance(value, dict):
        return _count(len(value), "key", "{", "}")
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    return _clip(escape_display_controls(repr(value)), MAX_VALUE_CHARS)


def _count(count: int, noun: str, opening: str, closing: str) -> str:
    if not count:
        return opening + closing
    return f"{opening}{count} {noun}{'s' if count != 1 else ''}{closing}"


def _fit(opening: str, pieces: List[str], closing: str, width: int) -> str:
    """Join as many pieces as fit, noting how many were left out."""
    shown: List[str] = []
    for index, piece in enumerate(pieces):
        remaining = len(pieces) - index - 1
        suffix = f", +{remaining} more" if remaining else ""
        candidate = opening + ", ".join([*shown, piece]) + suffix + closing
        if shown and len(candidate) > width:
            break
        shown.append(piece)
    hidden = len(pieces) - len(shown)
    suffix = f", +{hidden} more" if hidden else ""
    return _clip(opening + ", ".join(shown) + suffix + closing, width)


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(width - 1, 0)] + _ELLIPSIS
