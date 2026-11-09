"""The operator's MCP server inventory: `mcp.json` loading, placeholders and edits.

The file uses Claude Code's `mcpServers` shape so an entry can be copied between
the two unchanged. It is never discovered in the working directory: operators run
Kerkov inside cloned target repositories, and a planted `.mcp.json` would start an
attacker's command.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from kerkov.core.credentials import SENSITIVE_CHILD_ENV_VARS
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.private_files import prepare_private_parent
from kerkov.core.project_paths import bundled_asset_path

logger = get_logger("McpConfig")

CHECKOUT_CONFIG_NAME = "config/mcp.json"
USER_CONFIG_PATH = Path.home() / ".kerkov" / "mcp.json"
DEFAULT_TOOL_TIMEOUT_SECONDS = 300.0
MAX_SERVER_KEY_LENGTH = 32

STDIO = "stdio"
HTTP = "http"
_SUPPORTED_TRANSPORTS = (STDIO, HTTP)
_KNOWN_TRANSPORTS = (STDIO, HTTP, "sse")

_SERVER_KEY_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_SERVER_KEY_LENGTH - 1}}}"
)
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRET_NAME_RE = re.compile(
    r"(?i)authorization|api[-_]?key|token|secret|password|passwd|cookie|bearer"
)
_KNOWN_KEYS = frozenset(
    {
        "type",
        "command",
        "args",
        "env",
        "url",
        "headers",
        "enabled",
        "optional",
        "tools",
        "timeout",
    }
)


class McpConfigError(ValueError):
    """`mcp.json` or one of its entries cannot be used as written."""


class MissingKeyError(McpConfigError):
    """A required `${NAME}` placeholder has no value anywhere."""

    def __init__(self, names: List[str]) -> None:
        self.names = sorted(set(names))
        super().__init__("missing key: " + ", ".join(self.names))


@dataclass(frozen=True)
class ToolFilter:
    """Per-server `tools.allow`/`tools.deny` globs applied before advertising."""

    allow: Optional[Tuple[str, ...]] = None
    deny: Tuple[str, ...] = ()

    def permits(self, tool_name: str) -> bool:
        if any(fnmatchcase(tool_name, pattern) for pattern in self.deny):
            return False
        if self.allow is None:
            return True
        return any(fnmatchcase(tool_name, pattern) for pattern in self.allow)


@dataclass(frozen=True)
class McpServerConfig:
    """One `mcpServers` entry as written, placeholders unresolved."""

    key: str
    transport: str
    command: Optional[str] = None
    args: Tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    headers: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True
    optional: frozenset[str] = frozenset()
    tools: ToolFilter = ToolFilter()
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS

    @property
    def supported(self) -> bool:
        return self.transport in _SUPPORTED_TRANSPORTS

    def placeholders(self) -> List[str]:
        """Every `${NAME}` the entry references, in first-seen order."""
        values = [self.command or "", self.url or "", *self.args]
        values += [*self.env.values(), *self.headers.values()]
        names: List[str] = []
        for value in values:
            for match in _PLACEHOLDER_RE.finditer(value):
                if match.group(1) not in names:
                    names.append(match.group(1))
        return names


@dataclass(frozen=True)
class ResolvedServer:
    """A server entry with placeholders substituted; holds secrets, never log it."""

    config: McpServerConfig
    command: Optional[str]
    args: Tuple[str, ...]
    env: Dict[str, str]
    url: Optional[str]
    headers: Dict[str, str]
    key_sources: Dict[str, str]
    absent_optional: Tuple[str, ...]

    def __repr__(self) -> str:
        return f"ResolvedServer(key={self.config.key!r}, transport={self.config.transport!r})"


@dataclass(frozen=True)
class McpConfig:
    """The parsed file: usable servers, per-server errors and file-level warnings."""

    path: Path
    servers: Tuple[McpServerConfig, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()

    def server(self, key: str) -> Optional[McpServerConfig]:
        return next((s for s in self.servers if s.key == key), None)


def resolve_config_path(configured: Optional[str] = None) -> Path:
    """`MCP_CONFIG_PATH`, else a source checkout's `config/mcp.json`, else `~/.kerkov/mcp.json`."""
    if configured:
        return Path(configured).expanduser()
    checkout = bundled_asset_path(CHECKOUT_CONFIG_NAME)
    if checkout.is_file():
        return checkout
    return USER_CONFIG_PATH


def load_mcp_config(path: Path) -> McpConfig:
    """Parse `mcp.json`; a missing file is an empty inventory, a broken one raises."""
    raw = _read_document(path)
    servers: List[McpServerConfig] = []
    errors: Dict[str, str] = {}
    warnings: List[str] = []
    for key, entry in raw.get("mcpServers", {}).items():
        try:
            server, entry_warnings = parse_server(key, entry)
        except McpConfigError as exc:
            errors[str(key)] = str(exc)
            continue
        servers.append(server)
        warnings.extend(entry_warnings)
    return McpConfig(
        path=path, servers=tuple(servers), errors=errors, warnings=tuple(warnings)
    )


def parse_server(key: Any, entry: Any) -> Tuple[McpServerConfig, List[str]]:
    """Validate one `mcpServers` entry and return it with any warnings."""
    validate_server_key(key)
    if not isinstance(entry, Mapping):
        raise McpConfigError("entry must be a JSON object")
    warnings = [
        f"{key}: unknown key {name!r} ignored"
        for name in entry
        if name not in _KNOWN_KEYS
    ]

    transport = entry.get("type") or (
        STDIO if "command" in entry else HTTP if "url" in entry else None
    )
    if transport not in _KNOWN_TRANSPORTS:
        raise McpConfigError(
            f"unknown transport {transport!r}; use 'stdio' or 'http'"
            if transport
            else "needs a 'command' (stdio) or a 'url' (http)"
        )

    command = _optional_str(entry, "command")
    url = _optional_str(entry, "url")
    if transport == STDIO and not command:
        raise McpConfigError("stdio servers need a 'command'")
    if transport != STDIO and not url:
        raise McpConfigError(f"{transport} servers need a 'url'")

    env = _str_map(entry, "env")
    headers = _str_map(entry, "headers")
    warnings += _literal_secret_warnings(key, "env", env)
    warnings += _literal_secret_warnings(key, "headers", headers)
    if headers and url and _is_plain_remote_http(url):
        warnings.append(
            f"{key}: headers are sent over plain HTTP to a non-loopback host; "
            "use https"
        )

    server = McpServerConfig(
        key=key,
        transport=transport,
        command=command,
        args=tuple(_str_list(entry, "args")),
        env=env,
        url=url,
        headers=headers,
        enabled=_bool(entry, "enabled", True),
        optional=frozenset(_str_list(entry, "optional")),
        tools=_tool_filter(entry.get("tools")),
        timeout_seconds=_timeout(entry.get("timeout")),
    )
    return server, warnings


def validate_server_key(key: Any) -> None:
    """Server keys become part of tool names, so they stay short and unambiguous."""
    if not isinstance(key, str) or not _SERVER_KEY_RE.fullmatch(key):
        raise McpConfigError(
            f"server name {key!r} must be 1-{MAX_SERVER_KEY_LENGTH} letters, digits, "
            "'_' or '-', starting with a letter or digit"
        )
    if "__" in key:
        raise McpConfigError(
            f"server name {key!r} must not contain '__', the tool-name separator"
        )


def resolve_server(
    server: McpServerConfig, lookup: Callable[[str], Tuple[Optional[str], str]]
) -> ResolvedServer:
    """Substitute `${NAME}` / `${NAME:-default}`; `lookup` returns (value, source).

    An absent optional key drops the env entry, header or argument that uses it
    rather than sending an empty value. `command` and `url` cannot be dropped, so
    there every placeholder is required.
    """
    resolved: Dict[str, Optional[str]] = {}
    sources: Dict[str, str] = {}
    missing: List[str] = []

    def value_of(name: str) -> Optional[str]:
        if name not in resolved:
            value, source = lookup(name)
            resolved[name] = value
            if value is not None:
                sources[name] = source
        return resolved[name]

    def substitute(text: str, *, droppable: bool) -> Optional[str]:
        dropped = False

        def replace(match: "re.Match[str]") -> str:
            nonlocal dropped
            name, default = match.group(1), match.group(2)
            value = value_of(name)
            if value is not None:
                return value
            if default is not None:
                return default
            if droppable and name in server.optional:
                dropped = True
                return ""
            missing.append(name)
            return ""

        result = _PLACEHOLDER_RE.sub(replace, text)
        return None if dropped else result

    command = substitute(server.command, droppable=False) if server.command else None
    url = substitute(server.url, droppable=False) if server.url else None
    args = [substitute(arg, droppable=True) for arg in server.args]
    env = {k: substitute(v, droppable=True) for k, v in server.env.items()}
    headers = {k: substitute(v, droppable=True) for k, v in server.headers.items()}
    if missing:
        raise MissingKeyError(missing)

    stripped = sorted(name for name in env if name in SENSITIVE_CHILD_ENV_VARS)
    if stripped:
        logger.warning(
            "MCP server %s: not passing Kerkov-managed credentials %s",
            server.key,
            ", ".join(stripped),
        )
    absent = tuple(
        name
        for name in server.placeholders()
        if name in server.optional and resolved.get(name) is None
    )
    return ResolvedServer(
        config=server,
        command=command,
        args=tuple(a for a in args if a is not None),
        env={
            k: v
            for k, v in env.items()
            if v is not None and k not in SENSITIVE_CHILD_ENV_VARS
        },
        url=url,
        headers={k: v for k, v in headers.items() if v is not None},
        key_sources=sources,
        absent_optional=absent,
    )


def add_server(path: Path, key: str, entry: Mapping[str, Any]) -> None:
    """Add a server entry, keeping every other key in the file as written."""
    parse_server(key, entry)
    document = _read_document(path)
    servers = document.setdefault("mcpServers", {})
    if key in servers:
        raise McpConfigError(f"server {key!r} already exists in {path}")
    servers[key] = dict(entry)
    _write_document(path, document)


def remove_server(path: Path, key: str) -> None:
    """Remove a server entry, keeping every other key in the file as written."""
    document = _read_document(path)
    servers = document.get("mcpServers", {})
    if key not in servers:
        raise McpConfigError(f"no server named {key!r} in {path}")
    del servers[key]
    _write_document(path, document)


def _read_document(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"mcpServers": {}}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise McpConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(
        document.get("mcpServers", {}), dict
    ):
        raise McpConfigError(f"{path}: expected an object with an 'mcpServers' object")
    return document


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    prepare_private_parent(path.parent, managed_parent=USER_CONFIG_PATH.parent)
    descriptor, name = tempfile.mkstemp(prefix=".mcp-", dir=str(path.parent))
    temporary = Path(name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _is_plain_remote_http(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1")


def _literal_secret_warnings(
    key: str, section: str, values: Mapping[str, str]
) -> List[str]:
    return [
        f"{key}: {section}.{name} looks like a literal secret; "
        f"use a placeholder such as ${{{name.upper().replace('-', '_')}}}"
        for name, value in values.items()
        if _SECRET_NAME_RE.search(name) and value and not _PLACEHOLDER_RE.search(value)
    ]


def _optional_str(entry: Mapping[str, Any], name: str) -> Optional[str]:
    value = entry.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpConfigError(f"'{name}' must be a string")
    return value


def _str_list(entry: Mapping[str, Any], name: str) -> List[str]:
    value = entry.get(name, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise McpConfigError(f"'{name}' must be a list of strings")
    return list(value)


def _str_map(entry: Mapping[str, Any], name: str) -> Dict[str, str]:
    value = entry.get(name, {})
    if not isinstance(value, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise McpConfigError(f"'{name}' must map strings to strings")
    return dict(value)


def _bool(entry: Mapping[str, Any], name: str, default: bool) -> bool:
    value = entry.get(name, default)
    if not isinstance(value, bool):
        raise McpConfigError(f"'{name}' must be true or false")
    return value


def _tool_filter(value: Any) -> ToolFilter:
    if value is None:
        return ToolFilter()
    if not isinstance(value, Mapping):
        raise McpConfigError("'tools' must be an object with 'allow' and/or 'deny'")
    allow = _str_list(value, "allow") if "allow" in value else None
    return ToolFilter(
        allow=tuple(allow) if allow is not None else None,
        deny=tuple(_str_list(value, "deny")),
    )


def _timeout(value: Any) -> float:
    if value is None:
        return DEFAULT_TOOL_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise McpConfigError("'timeout' must be a positive number of seconds")
    return float(value)
