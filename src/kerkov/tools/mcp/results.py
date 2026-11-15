"""Turn an MCP `CallToolResult` into a Kerkov `ToolResult`.

`ToolResult.output` is one string, while MCP returns typed content blocks. Binary
blocks go to the run workspace and only their path reaches the conversation;
base64 in history would be unreadable and expensive.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from kerkov.core.run_workspace import get_current_workspace
from kerkov.tools.base_tool import ToolResult

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.-]")


def adapt_call_result(
    result: Any,
    *,
    server_key: str,
    tool_name: str,
    call_id: Optional[str],
    workspace: Optional[Path] = None,
) -> ToolResult:
    """Join text, save binary content, and fall back to `structured_content`."""
    writer = _BlobWriter(
        workspace if workspace is not None else get_current_workspace(),
        server_key,
        call_id,
    )
    parts = [_render_block(block, writer) for block in (result.content or [])]
    parts = [part for part in parts if part]
    structured = getattr(result, "structured_content", None)
    # Servers SHOULD mirror structured content as a text block; only fall back
    # to it when they did not, so the same payload is not shown twice.
    if not parts and structured is not None:
        parts.append(json.dumps(structured, indent=2, ensure_ascii=False, default=str))

    output = "\n\n".join(parts)
    metadata: Dict[str, Any] = {"mcp_server": server_key, "mcp_tool": tool_name}
    if writer.saved:
        metadata["files"] = writer.saved
    if getattr(result, "is_error", False):
        return ToolResult(
            success=False,
            output=output,
            error=output or f"MCP tool {tool_name} reported an error",
            metadata=metadata,
        )
    return ToolResult(success=True, output=output, metadata=metadata)


def _render_block(block: Any, writer: "_BlobWriter") -> str:
    kind = getattr(block, "type", None)
    if kind == "text":
        return block.text
    if kind in ("image", "audio"):
        return writer.save(kind, block.data, block.mime_type)
    if kind == "resource_link":
        label = f"[resource link: {block.uri}"
        if getattr(block, "name", None):
            label += f" ({block.name})"
        description = getattr(block, "description", None)
        return label + "]" + (f"\n{description}" if description else "")
    if kind == "resource":
        resource = block.resource
        text = getattr(resource, "text", None)
        if text is not None:
            return f"[resource: {resource.uri}]\n{text}"
        return writer.save(
            "resource", getattr(resource, "blob", ""), resource.mime_type, resource.uri
        )
    return f"[unsupported MCP content block: {kind}]"


class _BlobWriter:
    """Saves decoded blocks as `<workspace>/mcp/<server>/<call>-<n><ext>`."""

    def __init__(
        self, workspace: Optional[Path], server_key: str, call_id: Optional[str]
    ) -> None:
        self.workspace = workspace
        self.server_key = server_key
        # The workspace is shared by the whole agent tree; the call id keeps
        # concurrent agents from overwriting each other's files.
        self.stem = _UNSAFE_FILENAME.sub("_", call_id or "") or uuid.uuid4().hex[:12]
        self.saved: List[str] = []

    def save(
        self,
        kind: str,
        data: str,
        mime_type: Optional[str],
        uri: Optional[str] = None,
    ) -> str:
        source = f" from {uri}" if uri else ""
        mime = mime_type or "application/octet-stream"
        try:
            payload = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return f"[{kind}{source}: invalid base64 content omitted]"
        if self.workspace is None:
            return (
                f"[{kind}{source}: {mime}, {len(payload)} bytes omitted; "
                "no run workspace is active]"
            )
        extension = mimetypes.guess_extension(mime) or ".bin"
        directory = self.workspace / "mcp" / self.server_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.stem}-{len(self.saved) + 1}{extension}"
        path.write_bytes(payload)
        self.saved.append(str(path))
        return f"[{kind}{source} saved to {path} ({mime}, {len(payload)} bytes)]"
