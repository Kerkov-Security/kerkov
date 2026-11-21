"""`/mcp` in the web console, and server status for the Settings panel."""

from __future__ import annotations

from typing import Any, Optional

from kerkov.core.mcp.manager import McpManager, ServerStatus
from kerkov.web.runtime.providers import REQUEST_ID_PATTERN

MCP_REQUESTS = frozenset({"mcp.status"})
_FIELDS = frozenset({"type", "request_id"})
_USAGE = (
    "Use `/mcp`, `/mcp list [server]`, `/mcp connect [server]` or "
    "`/mcp disconnect <server>`."
)
_CACHE_NOTE = (
    "The advertised tool list changed; the next request rebuilds the provider's "
    "prompt cache."
)
_ENTRY = (
    "Keys and OAuth sign-ins are entered in the CLI, never in the browser. Run "
    "`/mcp {verb}` there, then `/mcp connect` here; this console uses what the CLI "
    "stored."
)
_EDIT = (
    "This console does not edit mcp.json. Edit the file or run `/mcp {verb}` in the "
    "CLI, then `/mcp connect` here."
)
_CLI_ONLY = {
    "key": _ENTRY,
    "login": _ENTRY,
    "logout": _ENTRY,
    "add": _EDIT,
    "remove": _EDIT,
}


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


async def run_mcp_command(session: Any, message: str) -> None:
    """Status, list, connect and disconnect; key entry and edits stay in the CLI."""
    verb, *rest = message.split()[1:] or ["status"]
    verb = verb.lower()
    manager: Optional[McpManager] = session.mcp
    if verb in _CLI_ONLY:
        _result(
            session,
            f"/mcp {verb} runs in the CLI",
            _CLI_ONLY[verb].format(verb=verb),
            failed=True,
        )
    elif manager is None:
        _result(
            session,
            "MCP is disabled",
            "Set MCP_ENABLED=true and restart Kerkov.",
            failed=True,
        )
    elif verb == "status" and not rest:
        _result(session, "MCP servers", "\n".join(_status_lines(manager)))
    elif verb == "list" and len(rest) <= 1:
        _list(session, manager, rest[0] if rest else None)
    elif verb == "connect" and len(rest) <= 1:
        await _connect(session, manager, rest[0] if rest else None)
    elif verb == "disconnect" and len(rest) == 1:
        await _disconnect(session, manager, rest[0])
    else:
        _result(session, "Invalid /mcp command", _USAGE, failed=True)


def _status_lines(manager: McpManager) -> list[str]:
    lines = [f"Config: {manager.config_path}"]
    if manager.load_error:
        return lines + [manager.load_error]
    rows = manager.status()
    if not rows:
        return lines + ["No MCP servers configured."]
    for row in rows:
        tools = (
            f"{row.exposed} of {row.advertised} tools"
            if row.advertised is not None
            else "no tools listed"
        )
        lines.append(
            f"{row.key} · {row.transport} · {row.state} · auth: {row.auth} · {tools}"
        )
    return lines


def _list(session: Any, manager: McpManager, server: Optional[str]) -> None:
    rows = [row for row in manager.status() if server in (None, row.key)]
    if not rows:
        _result(session, f"No MCP server named {server}", "", failed=True)
        return
    lines: list[str] = []
    for row in rows:
        lines.append(f"{row.key} ({row.state})")
        lines.extend(
            f"  {mapped} ← {original}" for mapped, original in sorted(row.tools.items())
        )
        lines.extend(f"  skipped {name}: {reason}" for name, reason in row.skipped)
        if not row.tools and not row.skipped:
            lines.append("  no tools exposed")
    _result(session, "MCP tools", "\n".join(lines))


async def _connect(session: Any, manager: McpManager, server: Optional[str]) -> None:
    result = await manager.reconnect(server)
    if result.changed:
        session.register_mcp_tools()
    lines = [f"Disconnected {key} (no longer in mcp.json)" for key in result.removed]
    if manager.load_error:
        lines.append(manager.load_error)
    elif not result.attempted:
        lines.append("Every enabled MCP server is already connected.")
    for key, error in result.attempted.items():
        lines.append(f"{key}: {error}" if error else f"Connected {key}")
    if result.changed:
        lines.append(_CACHE_NOTE)
    failed = bool(manager.load_error) or any(result.attempted.values())
    _result(session, "MCP connect", "\n".join(lines), failed=failed)


async def _disconnect(session: Any, manager: McpManager, server: str) -> None:
    if not await manager.disconnect(server):
        _result(session, f"{server} is not connected", "", failed=True)
        return
    session.register_mcp_tools()
    _result(session, f"Disconnected {server}", _CACHE_NOTE)


def _result(session: Any, title: str, detail: str, *, failed: bool = False) -> None:
    # The status rides along so the Settings panel never shows a stale tool set.
    session.bus.emit(
        "mcp.result",
        {
            "title": title,
            "detail": detail,
            "failed": failed,
            "status": mcp_status(session.mcp),
        },
    )
