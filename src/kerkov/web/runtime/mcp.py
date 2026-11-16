"""MCP server status for the console's read-only Settings panel."""

from __future__ import annotations

from typing import Any, Optional

from kerkov.core.mcp.manager import McpManager, ServerStatus
from kerkov.web.runtime.providers import REQUEST_ID_PATTERN

MCP_REQUESTS = frozenset({"mcp.status"})
_FIELDS = frozenset({"type", "request_id"})


def mcp_status(manager: Optional[McpManager]) -> dict[str, Any]:
    """What `/mcp` shows in the CLI; auth states name a source, never a value."""
    if manager is None:
        return {"enabled": False, "servers": [], "warnings": []}
    return {
        "enabled": True,
        "config_path": str(manager.config_path),
        "load_error": manager.load_error,
        "warnings": list(manager.warnings()),
        "servers": [_server(row) for row in manager.status()],
    }


def handle_mcp_message(session: Any, content: dict[str, Any]) -> None:
    request_id = content.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        session.bus.emit(
            "mcp.error", {"request_id": "", "detail": "Invalid MCP request ID."}
        )
        return
    if content.get("type") not in MCP_REQUESTS or set(content) - _FIELDS:
        session.bus.emit(
            "mcp.error", {"request_id": request_id, "detail": "Invalid MCP request."}
        )
        return
    session.bus.emit(
        "mcp.status", {"request_id": request_id, **mcp_status(session.mcp)}
    )


def _server(row: ServerStatus) -> dict[str, Any]:
    return {
        "key": row.key,
        "transport": row.transport,
        "enabled": row.enabled,
        "state": row.state,
        "auth": row.auth,
        "advertised": row.advertised,
        "exposed": row.exposed,
        "tools": [
            {"name": name, "original": original}
            for name, original in sorted(row.tools.items())
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in row.skipped],
    }
