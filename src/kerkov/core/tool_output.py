"""Reference formats for tool output that is kept out of the conversation.

These build the text the model reads in place of a body it can no longer see —
at ingest (`build_clipped_*`, when a result is too large for history) and at
compaction (`build_evicted_*`, when an older result is dropped from it). They
live here rather than beside either caller so `ConversationEngine` and
`ContextCompactor`, which already depend on each other, share one definition.
"""

from typing import Any, Dict, Optional, Tuple

# The line `format_tool_result` puts immediately before the output body. It
# is the last field it renders, so everything after this marker is output and
# eviction can replace it without touching status, error or metadata.
TOOL_OUTPUT_MARKER = "Output:\n"

PREVIEW_HEAD_CHARS = 1500
PREVIEW_TAIL_CHARS = 500
EVICTED_HEAD_CHARS = 400
EVICTED_TAIL_CHARS = 200
METADATA_VALUE_MAX_CHARS = 800


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def build_clipped_output(output: str, artifact_id: int) -> str:
    """Replace a large tool output with a reference plus a head/tail preview.

    The full text is already in the knowledge base by this point; what goes
    into history is the handle for getting it back plus enough of the content
    to judge whether that is worth doing.
    """
    size = len(output.encode("utf-8"))
    head = output[:PREVIEW_HEAD_CHARS]
    # The clip gate upstream counts bytes while these slices count characters,
    # so a multi-byte body can trip it while being shorter than head + tail;
    # taking a tail then would repeat text the head already holds.
    tail = (
        output[-PREVIEW_TAIL_CHARS:]
        if len(output) > PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS
        else ""
    )
    elided = len(output) - len(head) - len(tail)

    parts = [
        f"[{format_bytes(size)} of output stored as artifact#{artifact_id} and "
        f"kept out of this conversation. Retrieve the full text with "
        f"recall(query='artifact#{artifact_id}'), or search it with "
        f"recall(query='<keywords>'). Preview follows.]",
        "",
        head,
    ]
    if tail:
        parts.extend(
            [
                f"\n[... {elided} characters elided — in artifact#{artifact_id} ...]\n",
                tail,
            ]
        )
    return "\n".join(parts)


def build_clipped_metadata(
    metadata: Optional[Dict[str, Any]], artifact_id: int
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
        clipped[key] = (
            f"[{format_bytes(len(rendered.encode('utf-8')))} of '{key}' kept out "
            f"of this conversation; the tool's full output is stored as "
            f"artifact#{artifact_id}]"
        )
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
