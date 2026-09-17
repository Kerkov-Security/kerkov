"""Normalization and reference formats for tool output in conversation history.

These build previews and references at ingest (`normalize_tool_output` and
`build_clipped_*`, when a result is too large for history) and at compaction
(`build_evicted_*`, when an older result is dropped from it). They live here
rather than beside either caller so `ConversationEngine` and `ContextCompactor`,
which already depend on each other, share one definition.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple

# The line `format_tool_result` puts immediately before the output body. It
# is the last field it renders, so everything after this marker is output and
# eviction can replace it without touching status, error or metadata.
TOOL_OUTPUT_MARKER = "Output:\n"

EVICTED_HEAD_CHARS = 400
EVICTED_TAIL_CHARS = 200
METADATA_VALUE_MAX_CHARS = 800
TOOL_OUTPUT_MAX_CHARS = 6000
TOOL_OUTPUT_HEAD_CHARS = 1500
TOOL_OUTPUT_TAIL_CHARS = 500
_LINE_ENDINGS = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


@dataclass(frozen=True)
class ToolOutputNormalization:
    """Character counts describing the changes made to a history preview."""

    original_size: int
    was_clipped: bool = False
    lines_omitted: int = 0
    # Counted after repeated-line collapse, excluding any annotation text.
    elided_size: int = 0


def _line_spans(output: str) -> Iterator[Tuple[int, int, int]]:
    """Yield line start, body end and full end, then an empty sentinel."""
    start = 0
    for ending in _LINE_ENDINGS.finditer(output):
        yield start, ending.start(), ending.end()
        start = ending.end()
    if start < len(output):
        yield start, len(output), len(output)
    yield len(output), len(output), len(output)


def _collapse_repeated_lines(output: str) -> Tuple[str, int]:
    """Collapse adjacent runs without treating distinct findings as similar."""
    # Scan offsets instead of retaining a string for every short output line.
    # Retain untouched regions as whole slices, and only create a marker when a
    # run actually saves space.
    parts = []
    omitted = 0
    kept_until = 0
    run_key = None
    run_count = 0
    run_start = first_end = last_body_end = last_end = 0
    for start, body_end, end in _line_spans(output):
        key = output[start:body_end].rstrip(" \t") if end > start else None
        if key == run_key:
            run_count += 1
        else:
            if run_count >= 3:
                ending = output[last_body_end:last_end]
                marker = f"… ({run_count - 1} similar lines omitted) …{ending}"
                if first_end - run_start + len(marker) < last_end - run_start:
                    parts.append(output[kept_until:first_end])
                    parts.append(marker)
                    kept_until = last_end
                    omitted += run_count - 1
            run_key = key
            run_start, first_end = start, end
            run_count = 1
        last_body_end, last_end = body_end, end

    if not omitted:
        return output, 0
    parts.append(output[kept_until:])
    return "".join(parts), omitted


def normalize_tool_output(
    output: str,
    *,
    max_chars: int = TOOL_OUTPUT_MAX_CHARS,
    head: int = TOOL_OUTPUT_HEAD_CHARS,
    tail: int = TOOL_OUTPUT_TAIL_CHARS,
) -> Tuple[str, ToolOutputNormalization]:
    """Build a bounded history preview while leaving small outputs unchanged.

    Thresholds and counts use Unicode characters, not encoded bytes. Above the
    threshold, collapse runs of at least three identical lines, ignoring only
    trailing spaces and tabs. Then retain the configured head and tail if the
    collapsed body is still oversized. Their combined content budget is scaled
    down to ``max_chars`` when necessary; annotations and markers add a small
    overhead beyond that budget.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if head < 0 or tail < 0 or head + tail <= 0:
        raise ValueError("head and tail must be non-negative with a positive sum")

    original_size = len(output)
    if original_size <= max_chars:
        return output, ToolOutputNormalization(original_size=original_size)

    preview, lines_omitted = _collapse_repeated_lines(output)
    elided_size = 0
    if len(preview) > max_chars:
        if head + tail > max_chars:
            head = head * max_chars // (head + tail)
            tail = max_chars - head
        elided_size = len(preview) - head - tail
        head_text = preview[:head]
        tail_text = preview[-tail:] if tail else ""
        count_basis = " after repeated-line collapse" if lines_omitted else ""
        preview = (
            f"{head_text}\n"
            f"[… middle {elided_size} characters elided{count_basis} …]\n"
            f"{tail_text}"
        )

    summary = f"[Tool output clipped: {original_size} original characters"
    if lines_omitted:
        summary += f"; {lines_omitted} repeated lines omitted"
    summary += ".]"
    return f"{summary}\n{preview}", ToolOutputNormalization(
        original_size=original_size,
        was_clipped=True,
        lines_omitted=lines_omitted,
        elided_size=elided_size,
    )


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def build_clipped_output(
    output: str, artifact_id: int, *, normalized_preview: str
) -> str:
    """Replace a large tool output with a reference plus a head/tail preview.

    The full text is already in the knowledge base by this point; what goes
    into history is the handle for getting it back plus enough of the content
    to judge whether that is worth doing. The already-normalized preview is
    attached unchanged so there is only one preview policy.
    """
    size = len(output.encode("utf-8"))
    reference = (
        f"[{format_bytes(size)} of output stored as artifact#{artifact_id}. "
        f"Retrieve the full text with "
        f"recall(query='artifact#{artifact_id}'), or search it with "
        f"recall(query='<keywords>'). Preview follows.]"
    )
    return f"{reference}\n\n{normalized_preview}"


def build_clipped_metadata(
    metadata: Optional[Dict[str, Any]], artifact_id: Optional[int]
) -> Optional[Dict[str, Any]]:
    """Elide oversized metadata values, which history renders in full."""
    if not metadata:
        return metadata

    clipped: Dict[str, Any] = {}
    for key, value in metadata.items():
        rendered = repr(value)
        if len(rendered) <= METADATA_VALUE_MAX_CHARS:
            clipped[key] = value
            continue
        marker = (
            f"[{format_bytes(len(rendered.encode('utf-8')))} of '{key}' kept out "
            f"of this conversation"
        )
        if artifact_id is not None:
            marker += f"; the tool's full output is stored as artifact#{artifact_id}"
        clipped[key] = marker + "]"
    return clipped


def split_tool_output(content: str) -> Tuple[str, str]:
    """Split a formatted tool result into everything-but-the-output and the output.

    The header carries the tool name, status, error and metadata, which live
    only in the conversation message — the artifact behind it holds the output
    alone — so eviction has to keep it. Content that did not come from
    `format_tool_result` has no marker and is treated as all output.
    """
    header, marker, body = content.partition(TOOL_OUTPUT_MARKER)
    if not marker:
        return "", content
    return header + marker, body


def build_evicted_tool_reference(artifact_id: int) -> str:
    """Replace an already-ingested output body with its bare recall handle.

    Compaction is about to drop this message, so unlike `build_clipped_output`
    there is no preview to justify: everything the model can still act on is the
    artifact id. The caller keeps the header this stands after, so status,
    error and metadata — which the artifact does not hold — survive with it.
    """
    return (
        f"[Tool output evicted during context compaction. The full text is "
        f"stored as artifact#{artifact_id}; retrieve it with "
        f"recall(query='artifact#{artifact_id}').]"
    )


def build_evicted_tool_clip(output: str) -> str:
    """Head/tail clip for a tool result with no artifact behind it.

    The knowledge base was off or ingestion failed, so there is no handle to
    recall — what is dropped here is gone. Keep enough of both ends to identify
    what the call was and how it ended.
    """
    size = len(output.encode("utf-8"))
    head = output[:EVICTED_HEAD_CHARS]
    tail = (
        output[-EVICTED_TAIL_CHARS:]
        if len(output) > EVICTED_HEAD_CHARS + EVICTED_TAIL_CHARS
        else ""
    )
    elided = len(output) - len(head) - len(tail)

    parts = [
        f"[{format_bytes(size)} of tool output evicted during context "
        f"compaction and not recoverable. Preview follows.]",
        "",
        head,
    ]
    if tail:
        parts.extend([f"\n[... {elided} characters elided ...]\n", tail])
    return "\n".join(parts)
