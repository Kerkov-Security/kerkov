"""`/mcp`: inspect, connect and edit the operator's MCP servers."""

from __future__ import annotations

import contextlib
import os
import shlex
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from rich.markup import escape
from rich.table import Table

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.credentials import credential_spec_for_env_var
from kerkov.core.mcp.config import (
    HTTP,
    STDIO,
    McpConfigError,
    add_server,
    parse_server,
    remove_server,
)
from kerkov.core.mcp.oauth import LoopbackAuthorization, free_loopback_port

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

_USAGE = (
    "Use `/mcp`, `/mcp list [server]`, `/mcp connect [server]`, "
    "`/mcp disconnect <server>`, `/mcp key <server> [NAME]`, "
    "`/mcp login <server>`, `/mcp logout <server>`, "
    "`/mcp add <server> [-e KEY=VALUE ...] -- <command> [args...]`, "
    "`/mcp add <server> --url <url> [-H 'Name: value' ...]` or "
    "`/mcp remove <server>`."
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
        entry: Optional[Dict[str, Any]] = None,
        key_name: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.action = action
        self.server = server
        self.entry = entry or {}
        self.key_name = key_name
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
        if verb in ("disconnect", "remove", "login", "logout") and len(rest) == 1:
            return cls(action=verb, server=rest[0])
        if verb == "key" and len(rest) in (1, 2):
            return cls(
                action="key",
                server=rest[0],
                key_name=rest[1] if len(rest) == 2 else None,
            )
        if verb == "add":
            return cls._parse_add(rest)
        return cls(action="invalid")

    @classmethod
    def _parse_add(cls, tokens: List[str]) -> "McpMessage":
        command: List[str] = []
        if "--" in tokens:
            split = tokens.index("--")
            tokens, command = tokens[:split], tokens[split + 1 :]
        if not tokens:
            return cls(action="invalid", error="name the server")
        server, options = tokens[0], tokens[1:]
        env: Dict[str, str] = {}
        headers: Dict[str, str] = {}
        url: Optional[str] = None
        while options:
            flag = options.pop(0)
            value = options.pop(0) if options else None
            if flag in ("-e", "--env") and value and "=" in value:
                name, _, env_value = value.partition("=")
                env[name] = env_value
            elif flag in ("-H", "--header") and value and ":" in value:
                name, _, header = value.partition(":")
                headers[name.strip()] = header.strip()
            elif flag == "--url" and value:
                url = value
            else:
                return cls(action="invalid", error=f"unexpected option {flag!r}")
        if bool(command) == bool(url):
            return cls(
                action="invalid",
                error="give either `-- <command> [args...]` or `--url <url>`",
            )
        if (command and headers) or (url and env):
            return cls(
                action="invalid",
                error="`-e` applies to stdio servers and `-H` to HTTP servers",
            )
        entry: Dict[str, Any]
        if url:
            entry = {"type": HTTP, "url": url}
            if headers:
                entry["headers"] = headers
        else:
            entry = {"type": STDIO, "command": command[0], "args": command[1:]}
            if env:
                entry["env"] = env
        return cls(action="add", server=server, entry=entry)

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
        elif self.action == "key":
            await self._key(cli)
        elif self.action == "login":
            await self._login(cli)
        elif self.action == "logout":
            await self._logout(cli)
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
        manager.load()
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
        try:
            _, warnings = parse_server(self.server, self.entry)
            add_server(manager.config_path, self.server, self.entry)
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

    async def _key(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        manager.load()
        config = manager.server_config(self.server)
        if config is None:
            cli.console.print(
                f"[yellow]No MCP server named {escape(self.server)}.[/yellow]"
            )
            return
        names = config.placeholders()
        name = self.key_name or (names[0] if len(names) == 1 else None)
        if name is None or name not in names:
            listed = ", ".join(names) if names else "none"
            cli.console.print(
                f"[yellow]Name a key that {escape(self.server)} references in "
                f"its env or headers: {escape(listed)}.[/yellow]"
            )
            return
        try:
            spec = credential_spec_for_env_var(
                name, label=f"{name} for MCP server {self.server}"
            )
        except ValueError:
            cli.console.print(
                f"[yellow]{escape(name)} is not an upper-case variable name; set "
                "it in the environment instead.[/yellow]"
            )
            return
        credential = await cli.prompt_for_credential(spec)
        if credential is None:
            return
        result = cli.credentials.save(
            spec, credential.value, persist=credential.persist
        )
        if result.warning:
            cli.console.print(f"[yellow]{escape(result.warning)}[/yellow]")
        cli.console.print(f"[green]✓[/green] Saved {escape(name)}")
        self.action = "connect"
        await self._connect(cli)

    async def _login(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        manager.load()
        problem = manager.oauth_problem(self.server)
        if problem:
            cli.console.print(f"[yellow]{escape(problem)}.[/yellow]")
            return

        async def confirm_origin(origin: str) -> bool:
            cli.console.print(
                f"Authorization server for {escape(self.server or '')}: "
                f"[bold]{escape(origin)}[/bold]"
            )
            answer = await _ask(cli, "Sign in there? [Y/n] ")
            return answer is not None and answer.strip().lower() in ("", "y", "yes")

        async def paste_callback() -> str | None:
            cli.console.print(
                "The browser did not return to Kerkov. Paste the URL it ended on "
                "(or just the code), or leave it empty to cancel."
            )
            return await _ask(cli, "Callback URL or code: ", secret=True)

        login = LoopbackAuthorization(
            manager.login_port(self.server) or free_loopback_port(),
            confirm_origin=confirm_origin,
            paste_callback=paste_callback,
            notify=lambda text: cli.console.print(f"[dim]{escape(text)}[/dim]"),
        )
        error = await manager.connect(self.server, login=login)
        if error:
            cli.console.print(f"[red]{escape(self.server)}: {escape(error)}[/red]")
        else:
            cli.console.print(
                f"[green]✓[/green] Logged in and connected {escape(self.server)}"
            )
        cli.register_mcp_tools()
        cli.console.print(_CACHE_NOTE)

    async def _logout(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        manager.load()
        problem = manager.oauth_problem(self.server)
        if problem:
            cli.console.print(f"[yellow]{escape(problem)}.[/yellow]")
            return
        await manager.logout(self.server)
        cli.register_mcp_tools()
        cli.console.print(
            f"[green]✓[/green] Removed the stored login for {escape(self.server)}. "
            "[dim]This does not revoke it at the server.[/dim]"
        )

    async def _remove(self, cli: "KerkovCLI") -> None:
        manager = cli.mcp
        assert manager is not None and self.server is not None
        was_connected = await manager.disconnect(self.server)
        try:
            remove_server(manager.config_path, self.server)
        except McpConfigError as exc:
            cli.console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
            return
        manager.load()
        cli.register_mcp_tools()
        cli.console.print(f"[green]✓[/green] Removed {escape(self.server)}")
        if was_connected:
            cli.console.print(_CACHE_NOTE)


async def _ask(cli: "KerkovCLI", prompt: str, *, secret: bool = False) -> str | None:
    """One answer outside chat history; None when the operator cancels."""
    input_manager = cli.input_manager
    sink = getattr(cli, "output_sink", None)
    suspension = sink.suspend_status() if sink is not None else contextlib.nullcontext()
    with suspension:
        was_watching = input_manager.is_watching_for_interrupt
        if was_watching:
            input_manager.stop_watching_for_interrupt()
        try:
            if secret:
                return await input_manager.get_secret(prompt)
            return await input_manager.get_text(prompt)
        except (KeyboardInterrupt, EOFError):
            return None
        finally:
            if was_watching:
                input_manager.reset_interrupt()
                input_manager.start_watching_for_interrupt()


def _split(raw_input: str) -> List[str]:
    if os.name != "nt":
        return shlex.split(raw_input)
    # POSIX rules would eat the backslashes in Windows paths.
    return [_unquote(token) for token in shlex.split(raw_input, posix=False)]


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token
