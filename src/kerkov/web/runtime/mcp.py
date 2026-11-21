"""`/mcp` in the web console, and server status for the Settings panel."""

from __future__ import annotations

import shlex
from typing import Any, Optional

from kerkov.core.mcp.manager import McpManager, ServerStatus
from kerkov.core.mcp.prompts import (
    PromptError,
    describe_prompt,
    expand_prompt,
    parse_prompt_arguments,
)
from kerkov.web.runtime.providers import REQUEST_ID_PATTERN

MCP_REQUESTS = frozenset({"mcp.status"})
_FIELDS = frozenset({"type", "request_id"})
_USAGE = (
    "Use `/mcp`, `/mcp list [server]`, `/mcp prompts [server]`, "
    "`/mcp prompt <server> <prompt> [name=value ...]`, `/mcp connect [server]` or "
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
        "trusted": row.trusted,
        "resources": row.resources,
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
    elif verb == "prompts" and len(rest) <= 1:
        await _prompts(session, manager, rest[0] if rest else None)
    elif verb == "prompt":
        _result(
            session,
            "MCP prompt not loaded",
            "Name the server and the prompt: "
            "/mcp prompt <server> <prompt> [name=value ...]. /mcp prompts lists them.",
            failed=True,
        )
    elif verb == "connect" and len(rest) <= 1:
        await _connect(session, manager, rest[0] if rest else None)
    elif verb == "disconnect" and len(rest) == 1:
        await _disconnect(session, manager, rest[0])
    else:
        _result(session, "Invalid /mcp command", _USAGE, failed=True)


def is_prompt_command(message: str) -> bool:
    """`/mcp prompt <server> <prompt> ...`; shorter forms get a usage reply."""
    words = message.split(maxsplit=4)
    return len(words) >= 4 and words[0] == "/mcp" and words[1].lower() == "prompt"


async def expand_prompt_command(session: Any, message: str) -> Optional[str]:
    """The prompt's text for the next user turn, or None after reporting why not.

    The console cannot ask for a missing argument, so required ones must be given.
    """
    manager: Optional[McpManager] = session.mcp
    if manager is None:
        _result(
            session,
            "MCP is disabled",
            "Set MCP_ENABLED=true and restart Kerkov.",
            failed=True,
        )
        return None
    try:
        words = shlex.split(message)[2:]
        if len(words) < 2:
            raise PromptError("name the server and the prompt")
        server, name, *rest = words
        return await expand_prompt(manager, server, name, parse_prompt_arguments(rest))
    except (PromptError, ValueError) as exc:
        _result(session, "MCP prompt not loaded", str(exc), failed=True)
        return None


async def _prompts(session: Any, manager: McpManager, server: Optional[str]) -> None:
    listing = await manager.list_prompts(server)
    lines: list[str] = []
    current = None
    for prompt in listing.prompts:
        if prompt.server != current:
            current = prompt.server
            lines.append(current)
        lines.append(f"  {describe_prompt(prompt)}")
    lines += [f"{key}: {error}" for key, error in sorted(listing.errors.items())]
    if listing.prompts:
        lines.append("* required. Use /mcp prompt <server> <prompt> [name=value ...].")
    _result(
        session,
        "MCP prompts",
        "\n".join(lines) or "No connected MCP server offers prompts.",
        failed=bool(listing.errors) and not listing.prompts,
    )


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
        lines.append(f"{row.key} ({row.state}{', trusted' if row.trusted else ''})")
        lines.extend(
            f"  {mapped} ← {original}" for mapped, original in sorted(row.tools.items())
        )
        if row.resources:
            lines.append(f"  {row.resources} ← resources (list, read)")
        lines.extend(f"  skipped {name}: {reason}" for name, reason in row.skipped)
        if not row.tools and not row.skipped and not row.resources:
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
