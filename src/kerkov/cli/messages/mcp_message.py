"""`/mcp`: inspect, connect and edit the operator's MCP servers."""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING, Dict, List, Optional

from rich.markup import escape
from rich.table import Table

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.mcp.config import (
    STDIO,
    McpConfigError,
    add_server,
    parse_server,
    remove_server,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

_USAGE = (
    "Use `/mcp`, `/mcp list [server]`, `/mcp connect [server]`, "
    "`/mcp disconnect <server>`, `/mcp add <server> [-e KEY=VALUE ...] -- "
    "<command> [args...]` or `/mcp remove <server>`."
)
_CACHE_NOTE = (
    "[dim]The advertised tool list changed; the next request rebuilds the "
    "provider's prompt cache.[/dim]"
)


class McpMessage(UserMessage):
    """One `/mcp` action with its parsed arguments."""

    def __init__(
        self,
        action: str = "status",
        server: Optional[str] = None,
        command: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.action = action
        self.server = server
        self.command = command or []
        self.env = env or {}
        self.error = error

    @classmethod
    def parse(cls, raw_input: str) -> "McpMessage":
        try:
            tokens = _split(raw_input)[1:]
        except ValueError as exc:
            return cls(action="invalid", error=str(exc))
        if not tokens:
            return cls()
        verb, rest = tokens[0].lower(), tokens[1:]
        if verb == "status" and not rest:
            return cls()
        if verb in ("list", "connect") and len(rest) <= 1:
            return cls(action=verb, server=rest[0] if rest else None)
        if verb in ("disconnect", "remove") and len(rest) == 1:
            return cls(action=verb, server=rest[0])
        if verb == "add":
            return cls._parse_add(rest)
        return cls(action="invalid")

    @classmethod
    def _parse_add(cls, tokens: List[str]) -> "McpMessage":
        if "--" not in tokens:
            return cls(action="invalid", error="put the server command after `--`")
        split = tokens.index("--")
        head, command = tokens[:split], tokens[split + 1 :]
        if not head or not command:
            return cls(action="invalid", error="name the server and its command")
        server, options = head[0], head[1:]
        env: Dict[str, str] = {}
        while options:
            flag = options.pop(0)
            if flag not in ("-e", "--env") or not options or "=" not in options[0]:
                return cls(action="invalid", error=f"unexpected option {flag!r}")
            name, _, value = options.pop(0).partition("=")
            env[name] = value
        return cls(action="add", server=server, command=command, env=env)

    async def execute(self, cli: "KerkovCLI") -> None:
        if self.action == "invalid":
            detail = f"{escape(self.error)}. " if self.error else ""
            cli.console.print(f"[bold red]Error:[/bold red] {detail}{_USAGE}")
        elif cli.mcp is None:
            cli.console.print(
                "[yellow]MCP is disabled. Set MCP_ENABLED=true and restart.[/yellow]"
            )
        elif self.action == "status":
            self._status(cli)
        elif self.action == "list":
            self._list(cli)
        elif self.action == "connect":
            await self._connect(cli)
        elif self.action == "disconnect":
            await self._disconnect(cli)
        elif self.action == "add":
            await self._add(cli)
        elif self.action == "remove":
            await self._remove(cli)
        cli.console.print()

    def _status(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None
        cli.console.print(f"[dim]Config: {escape(str(manager.config_path))}[/dim]")
        if manager.load_error:
            cli.console.print(f"[red]{escape(manager.load_error)}[/red]")
            return
        rows = manager.status()
        if not rows:
            cli.console.print(
                "No MCP servers configured. Add one with "
                "`/mcp add <server> -- <command> [args...]`."
            )
            return
        table = Table(show_header=True, header_style="bold")
        for column in ("Server", "Transport", "State", "Auth", "Tools"):
            table.add_column(column)
        for row in rows:
            tools = (
                f"{row.exposed} of {row.advertised}"
                if row.advertised is not None
                else "-"
            )
            table.add_row(
                escape(row.key),
                row.transport,
                escape(row.state),
                escape(row.auth),
                tools,
            )
        cli.console.print(table)

    def _list(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None
        rows = [r for r in manager.status() if self.server in (None, r.key)]
        if not rows:
            cli.console.print(
                f"[yellow]No MCP server named {escape(self.server or '')}.[/yellow]"
            )
            return
        for row in rows:
            cli.console.print(
                f"[bold]{escape(row.key)}[/bold] [dim]({escape(row.state)})[/dim]"
            )
            for mapped, original in sorted(row.tools.items()):
                cli.console.print(f"  {escape(mapped)} [dim]← {escape(original)}[/dim]")
            for original, reason in row.skipped:
                cli.console.print(
                    f"  [yellow]skipped {escape(original)}: {escape(reason)}[/yellow]"
                )
            if not row.tools and not row.skipped:
                cli.console.print("  [dim]no tools exposed[/dim]")

    async def _connect(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None
        removed = await manager.reload()
        for key in removed:
            cli.console.print(
                f"[green]✓[/green] Disconnected {escape(key)} "
                "[dim](no longer in mcp.json)[/dim]"
            )
        if removed:
            cli.register_mcp_tools()
        if manager.load_error:
            cli.console.print(f"[red]{escape(manager.load_error)}[/red]")
            return
        if self.server is not None:
            keys = [self.server]
        else:
            keys = [
                row.key
                for row in manager.status()
                if row.enabled and row.state != "connected"
            ]
        if not keys:
            cli.console.print(
                "[dim]Every enabled MCP server is already connected.[/dim]"
            )
            if removed:
                cli.console.print(_CACHE_NOTE)
            return
        for key in keys:
            error = await manager.connect(key)
            if error:
                cli.console.print(f"[red]{escape(key)}: {escape(error)}[/red]")
            else:
                cli.console.print(f"[green]✓[/green] Connected {escape(key)}")
        cli.register_mcp_tools()
        cli.console.print(_CACHE_NOTE)

    async def _disconnect(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        if not await manager.disconnect(self.server):
            cli.console.print(
                f"[yellow]{escape(self.server)} is not connected.[/yellow]"
            )
            return
        cli.register_mcp_tools()
        cli.console.print(f"[green]✓[/green] Disconnected {escape(self.server)}")
        cli.console.print(_CACHE_NOTE)

    async def _add(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        entry: Dict[str, object] = {
            "type": STDIO,
            "command": self.command[0],
            "args": self.command[1:],
        }
        if self.env:
            entry["env"] = self.env
        try:
            _, warnings = parse_server(self.server, entry)
            add_server(manager.config_path, self.server, entry)
        except McpConfigError as exc:
            cli.console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
            return
        for warning in warnings:
            cli.console.print(f"[yellow]{escape(warning)}[/yellow]")
        cli.console.print(
            f"[green]✓[/green] Added {escape(self.server)} to "
            f"{escape(str(manager.config_path))}"
        )
        self.action = "connect"
        await self._connect(cli)

    async def _remove(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        was_connected = await manager.disconnect(self.server)
        try:
            remove_server(manager.config_path, self.server)
        except McpConfigError as exc:
            cli.console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
            return
        await manager.reload()
        cli.register_mcp_tools()
        cli.console.print(f"[green]✓[/green] Removed {escape(self.server)}")
        if was_connected:
            cli.console.print(_CACHE_NOTE)


def _split(raw_input: str) -> List[str]:
    if os.name != "nt":
        return shlex.split(raw_input)
    # POSIX rules would eat the backslashes in Windows paths.
    return [_unquote(token) for token in shlex.split(raw_input, posix=False)]


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token
