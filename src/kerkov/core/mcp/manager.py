"""Connect the configured MCP servers and expose their tools to a front end.

Tools are discovered once per connection and frozen: the advertised tool list sits
inside the provider's cached prompt prefix, so it changes only on an explicit,
visible connect or disconnect.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Any, Callable, Dict, List, Optional, Tuple

from kerkov.core.credentials import CredentialManager, credential_spec_for_env_var
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.mcp.config import (
    McpConfig,
    McpConfigError,
    McpServerConfig,
    MissingKeyError,
    ResolvedServer,
    load_mcp_config,
    resolve_server,
)
from kerkov.core.mcp.connection import (
    ClientFactory,
    HttpClientFactory,
    McpCallError,
    McpConnection,
    McpUnavailableError,
    require_sdk,
)
from kerkov.core.mcp.naming import map_tool_names
from kerkov.tools.mcp.mcp_tool import McpTool, SchemaNotRepresentableError

logger = get_logger("McpManager")

_CREDENTIAL_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")

ClientFactoryProvider = Callable[[ResolvedServer], Optional[ClientFactory]]


@dataclass
class ServerStatus:
    """One `/mcp` row: what is configured and what happened when connecting."""

    key: str
    transport: str
    enabled: bool
    state: str
    auth: str
    advertised: Optional[int] = None
    exposed: Optional[int] = None
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    tools: Dict[str, str] = field(default_factory=dict)


@dataclass
class _Server:
    config: McpServerConfig
    connection: Optional[McpConnection] = None
    tools: List[McpTool] = field(default_factory=list)
    advertised: Optional[int] = None
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    error: Optional[str] = None
    auth: str = "none"
    disconnected: bool = False


class McpManager:
    """Owns every MCP connection for one front-end process."""

    def __init__(
        self,
        config_path: Path,
        *,
        credentials: CredentialManager,
        client_factory_provider: Optional[ClientFactoryProvider] = None,
        http_client_factory: Optional[HttpClientFactory] = None,
        environment: Optional[Dict[str, str]] = None,
        on_warning: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config_path = config_path
        self._credentials = credentials
        self._client_factory_provider = client_factory_provider
        self._http_client_factory = http_client_factory
        self._on_warning = on_warning
        self._environment = environment if environment is not None else os.environ
        self._servers: Dict[str, _Server] = {}
        self._reserved: frozenset[str] = frozenset()
        self.config: Optional[McpConfig] = None
        self.load_error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self._servers) or bool(self.config and self.config.errors)

    def warnings(self) -> Tuple[str, ...]:
        return self.config.warnings if self.config else ()

    def load(self) -> None:
        """(Re)read `mcp.json`, keeping live connections for unchanged servers."""
        try:
            self.config = load_mcp_config(self.config_path)
            self.load_error = None
        except McpConfigError as exc:
            self.config = None
            self.load_error = str(exc)
            return
        current = {server.key: server for server in self.config.servers}
        for key in list(self._servers):
            if key not in current:
                del self._servers[key]
        for key, server in current.items():
            existing = self._servers.get(key)
            if existing is None or existing.connection is None:
                self._servers[key] = _Server(config=server)
            else:
                existing.config = server

    async def start(self, reserved_names: AbstractSet[str]) -> None:
        """Load the file and connect every enabled server concurrently."""
        self._reserved = frozenset(reserved_names)
        self.load()
        keys = [key for key, s in self._servers.items() if s.config.enabled]
        if keys:
            await asyncio.gather(*(self.connect(key) for key in keys))

    async def connect(self, key: str) -> Optional[str]:
        """Connect one server; returns an error message, or None when connected."""
        server = self._servers.get(key)
        if server is None:
            return f"no server named {key!r} in {self.config_path}"
        if server.connection is not None:
            await self.disconnect(key)
        server.error = None
        server.disconnected = False
        server.skipped = []
        server.advertised = None
        if not server.config.supported:
            server.error = (
                f"the {server.config.transport!r} transport is not supported yet"
            )
            return server.error
        try:
            if self._client_factory_provider is None:
                require_sdk()
            resolved = resolve_server(server.config, self._lookup)
        except MissingKeyError as exc:
            server.auth = "missing key"
            server.error = (
                f"{exc}; store it with `/auth set {exc.names[0]}` or set it in the "
                "environment, then `/mcp connect " + key + "`"
            )
            return server.error
        except (McpUnavailableError, McpConfigError) as exc:
            server.error = str(exc)
            return server.error
        server.auth = _auth_label(resolved)
        # Emitted here rather than carried on the status row: by the time a
        # caller could read a row, the header has already gone over the wire.
        for warning in resolved.warnings:
            self._warn(warning)

        factory = (
            self._client_factory_provider(resolved)
            if self._client_factory_provider is not None
            else None
        )
        connection = McpConnection(
            resolved,
            client_factory=factory,
            http_client_factory=self._http_client_factory,
        )
        server.connection = connection
        try:
            discovered = await connection.start()
        except McpCallError as exc:
            server.error = str(exc) or connection.describe_state()
            server.connection = None
            if connection.auth_failure is not None:
                server.auth = (
                    "rejected" if resolved.sends_credentials else "credentials required"
                )
            return server.error
        server.advertised = len(discovered)
        server.tools = self._build_tools(server, discovered)
        logger.info(
            "MCP server %s connected: %d tools advertised, %d exposed",
            key,
            len(discovered),
            len(server.tools),
        )
        return None

    async def disconnect(self, key: str) -> bool:
        server = self._servers.get(key)
        if server is None or server.connection is None:
            return False
        connection, server.connection = server.connection, None
        server.tools = []
        server.disconnected = True
        await connection.close()
        return True

    async def close(self) -> None:
        """Close every connection; quit paths call this so no server outlives Kerkov."""
        await asyncio.gather(
            *(self.disconnect(key) for key in list(self._servers)),
            return_exceptions=True,
        )

    def cancel_inflight(self) -> int:
        """Cancel running calls but keep connections (engagement switches)."""
        return sum(
            server.connection.cancel_calls()
            for server in self._servers.values()
            if server.connection is not None
        )

    def tools(self) -> List[McpTool]:
        """Every exposed tool, in a stable order for the cached prompt prefix."""
        tools = [tool for server in self._servers.values() for tool in server.tools]
        return sorted(tools, key=lambda tool: tool.name)

    def status(self) -> List[ServerStatus]:
        rows: List[ServerStatus] = []
        for key in sorted(self._servers):
            server = self._servers[key]
            if server.connection is not None:
                state = server.connection.describe_state()
            elif server.error:
                state = f"failed: {server.error}"
            elif server.disconnected:
                state = "disconnected"
            else:
                state = "not started" if server.config.enabled else "disabled"
            rows.append(
                ServerStatus(
                    key=key,
                    transport=server.config.transport,
                    enabled=server.config.enabled,
                    state=state,
                    auth=server.auth,
                    advertised=server.advertised,
                    exposed=(
                        len(server.tools) if server.advertised is not None else None
                    ),
                    skipped=list(server.skipped),
                    tools={tool.name: tool.original_name for tool in server.tools},
                )
            )
        if self.config:
            for key, error in sorted(self.config.errors.items()):
                rows.append(
                    ServerStatus(
                        key=key,
                        transport="?",
                        enabled=False,
                        state=f"invalid config: {error}",
                        auth="-",
                    )
                )
        return rows

    def server_config(self, key: str) -> Optional[McpServerConfig]:
        server = self._servers.get(key)
        return server.config if server else None

    async def _call(
        self, key: str, tool_name: str, arguments: Dict[str, Any], timeout: float
    ) -> Any:
        server = self._servers.get(key)
        if server is None or server.connection is None:
            raise McpCallError(
                f"MCP server {key} is not connected; reconnect with `/mcp connect {key}`"
            )
        return await server.connection.call_tool(tool_name, arguments, timeout)

    def _build_tools(self, server: _Server, discovered: List[Any]) -> List[McpTool]:
        permitted = [
            info for info in discovered if server.config.tools.permits(info.name)
        ]
        taken = set(self._reserved) | {
            tool.name
            for other in self._servers.values()
            if other is not server
            for tool in other.tools
        }
        by_original = {info.name: info for info in permitted}
        mapped = map_tool_names(server.config.key, by_original, taken)
        tools: List[McpTool] = []
        for name, original in sorted(mapped.items()):
            info = by_original[original]
            try:
                tools.append(
                    McpTool(
                        name=name,
                        server_key=server.config.key,
                        original_name=original,
                        description=info.description,
                        input_schema=info.input_schema,
                        caller=self._call,
                        timeout_seconds=server.config.timeout_seconds,
                    )
                )
            except SchemaNotRepresentableError as exc:
                server.skipped.append((original, str(exc)))
                logger.warning(
                    "MCP server %s: tool %s skipped, schema not representable: %s",
                    server.config.key,
                    original,
                    exc,
                )
        return tools

    def _warn(self, text: str) -> None:
        logger.warning("%s", text)
        if self._on_warning is not None:
            self._on_warning(text)

    def _lookup(self, name: str) -> Tuple[Optional[str], str]:
        if _CREDENTIAL_NAME.fullmatch(name):
            spec = credential_spec_for_env_var(name)
            value = self._credentials.get(spec)
            if value:
                return value, self._credentials.source(spec) or "environment"
            return None, ""
        value = self._environment.get(name)
        return (value, "environment") if value else (None, "")


def _auth_label(resolved: ResolvedServer) -> str:
    if resolved.key_sources:
        sources = sorted(set(resolved.key_sources.values()))
        label = "key (" + ", ".join(sources) + ")"
    elif resolved.sends_credentials:
        label = "key (config)"
    elif resolved.absent_optional:
        return "no key (optional)"
    else:
        return "none"
    if resolved.absent_optional:
        label += "; optional absent: " + ", ".join(resolved.absent_optional)
    return label
