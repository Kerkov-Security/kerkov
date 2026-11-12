"""One long-lived connection to one MCP server.

The SDK's `Client` is strictly `async with`, and anyio requires a cancel scope to
be entered and exited in the same task, so a holder task owns that block for the
connection's lifetime. Requests do not need to run in that task: callers use the
entered client directly, raced against the holder so a dead server fails the call
instead of hanging it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    IO,
    Any,
    AsyncContextManager,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Set,
)

from kerkov.core.logging.logger_factory import DEFAULT_LOGGING_DIR, get_logger
from kerkov.core.mcp.config import HTTP, STDIO, ResolvedServer, redacted_url

logger = get_logger("McpConnection")

CONNECT_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 30.0
# A server may hold a response stream open while a long tool call runs.
HTTP_READ_TIMEOUT_SECONDS = 300.0
AUTH_FAILURE_STATUSES = (401, 403)
_MAX_LIST_PAGES = 100
# `httpx2` is the MCP SDK's HTTP client and nothing else in Kerkov uses it.
_HTTP_LIBRARY_LOGGER = "httpx2"
_URL_ARGUMENT_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*://\S*\Z")

ClientFactory = Callable[[], AsyncContextManager[Any]]
HttpClientFactory = Callable[[Dict[str, str]], Any]


class McpUnavailableError(RuntimeError):
    """The optional `mcp` dependency is not installed."""


class McpCallError(RuntimeError):
    """A tool call could not be completed (protocol error, timeout, lost server)."""


class ConnectionState(str, Enum):
    NOT_STARTED = "not started"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True)
class McpToolInfo:
    """A tool as the server advertised it at discovery."""

    name: str
    description: str
    input_schema: Dict[str, Any]


def require_sdk() -> None:
    """Raise an actionable error when the optional MCP SDK is missing."""
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise McpUnavailableError(
            "MCP support is not installed. Install the 'mcp' extra: "
            "`uv sync --extra mcp` in a checkout, or `pip install 'kerkov[mcp]'`."
        ) from exc


def server_log_path(server_key: str) -> Path:
    """Where a stdio server's stderr goes, so it never draws over the prompt."""
    root = Path(os.environ.get("KERKOV_LOGGING_DIR", DEFAULT_LOGGING_DIR))
    return root / "mcp" / f"{server_key}.log"


class McpConnection:
    """Owns one server's client for as long as Kerkov keeps it connected."""

    def __init__(
        self,
        server: ResolvedServer,
        *,
        client_factory: Optional[ClientFactory] = None,
        http_client_factory: Optional[HttpClientFactory] = None,
    ) -> None:
        self.server = server
        self.key = server.config.key
        self.state = ConnectionState.NOT_STARTED
        self.error: Optional[str] = None
        # Set from the HTTP responses themselves: the SDK reports a 401 as a
        # generic JSON-RPC error, which would hide that credentials failed.
        self.auth_failure: Optional[int] = None
        self._client_factory = client_factory
        self._http_client_factory = http_client_factory
        self._client: Any = None
        self._holder: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._calls: Set[asyncio.Task[Any]] = set()
        self._errlog: Optional[IO[str]] = None

    async def start(
        self, timeout: float = CONNECT_TIMEOUT_SECONDS
    ) -> List[McpToolInfo]:
        """Connect, list every tool once, and keep the connection open."""
        if self._holder is not None:
            raise RuntimeError(f"MCP server {self.key} was already started")
        self.state = ConnectionState.CONNECTING
        ready: asyncio.Future[List[McpToolInfo]] = (
            asyncio.get_running_loop().create_future()
        )
        self._holder = asyncio.create_task(
            self._hold(ready), name=f"mcp-holder-{self.key}"
        )
        try:
            return await asyncio.wait_for(asyncio.shield(ready), timeout)
        except asyncio.TimeoutError:
            await self.close()
            self._fail(f"did not finish connecting within {timeout:g}s")
            raise McpCallError(self.error or "") from None
        except BaseException:
            await self.close()
            raise

    async def call_tool(
        self, name: str, arguments: Dict[str, Any], timeout: float
    ) -> Any:
        """Call one tool; returns the SDK's `CallToolResult`."""
        if self.state is not ConnectionState.CONNECTED or self._holder is None:
            raise McpCallError(
                f"MCP server {self.key} is not connected ({self.describe_state()})"
            )
        self.auth_failure = None
        call = asyncio.create_task(self._client.call_tool(name, arguments))
        self._calls.add(call)
        call.add_done_callback(self._calls.discard)
        try:
            done, _ = await asyncio.wait(
                {call, self._holder},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            call.cancel()
            raise
        if call not in done:
            call.cancel()
            if self._holder in done:
                raise McpCallError(
                    f"MCP server {self.key} disconnected during the call"
                    + (f": {self.error}" if self.error else "")
                )
            raise McpCallError(
                f"no result from MCP server {self.key} within {timeout:g}s; "
                "the server may still be working on it"
            )
        try:
            return call.result()
        except asyncio.CancelledError:
            raise McpCallError("the call was cancelled") from None
        except Exception as exc:
            message = self._auth_failure_message() or describe_exception(exc)
            raise McpCallError(message) from exc

    def cancel_calls(self) -> int:
        """Cancel every in-flight call without closing the connection."""
        pending = [call for call in self._calls if not call.done()]
        for call in pending:
            call.cancel()
        return len(pending)

    async def close(self) -> None:
        """Leave the client context in its own task, so the transport shuts down."""
        self.cancel_calls()
        holder = self._holder
        if holder is not None and not holder.done():
            self._stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(holder), CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("MCP server %s did not shut down; cancelling", self.key)
                holder.cancel()
                await asyncio.gather(holder, return_exceptions=True)
        if self.state is not ConnectionState.FAILED:
            self.state = ConnectionState.CLOSED
        self._client = None
        self._close_errlog()

    def describe_state(self) -> str:
        if self.state is ConnectionState.FAILED and self.error:
            return f"failed: {self.error}"
        return self.state.value

    async def _hold(self, ready: "asyncio.Future[List[McpToolInfo]]") -> None:
        try:
            async with self._open_client() as client:
                self._client = client
                tools = await _list_all_tools(client)
                self.state = ConnectionState.CONNECTED
                ready.set_result(tools)
                await self._stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            self._fail(self._auth_failure_message() or describe_exception(exc))
            if not ready.done():
                ready.set_exception(McpCallError(self.error or ""))
            else:
                logger.warning("MCP server %s disconnected: %s", self.key, self.error)
        finally:
            self._client = None

    def _open_client(self) -> AsyncContextManager[Any]:
        if self._client_factory is not None:
            return self._client_factory()
        require_sdk()
        transport = self.server.config.transport
        if transport == HTTP:
            return self._open_http_client()
        if transport != STDIO:
            raise McpCallError(f"the {transport!r} transport is not supported yet")
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.server.command or "",
            args=list(self.server.args),
            env=dict(self.server.env),
        )
        log_path = server_log_path(self.key)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._errlog = log_path.open("a", encoding="utf-8")
        return Client(stdio_client(params, errlog=self._errlog))

    @asynccontextmanager
    async def _open_http_client(self) -> AsyncIterator[Any]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        _redact_logged_urls()
        factory = self._http_client_factory or _default_http_client
        http = factory(dict(self.server.headers))
        http.event_hooks = {"response": [self._record_status]}
        async with http:
            transport = streamable_http_client(self.server.url or "", http_client=http)
            async with Client(transport) as client:
                yield client

    async def _record_status(self, response: Any) -> None:
        if response.status_code in AUTH_FAILURE_STATUSES:
            self.auth_failure = response.status_code

    def _auth_failure_message(self) -> Optional[str]:
        if self.auth_failure is None:
            return None
        return f"HTTP {self.auth_failure}: the server refused the request's credentials"

    def _fail(self, message: str) -> None:
        self.state = ConnectionState.FAILED
        self.error = message

    def _close_errlog(self) -> None:
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None


class _UrlRedactingFilter(logging.Filter):
    """Keeps a URL argument's userinfo and query out of the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redacted_argument(arg) for arg in record.args)
        return True


def _redact_logged_urls() -> None:
    """`httpx2` logs each request's full URL at INFO, and a `url` may hold a key."""
    library = logging.getLogger(_HTTP_LIBRARY_LOGGER)
    if not any(isinstance(f, _UrlRedactingFilter) for f in library.filters):
        library.addFilter(_UrlRedactingFilter())


def _redacted_argument(value: Any) -> Any:
    text = str(value)
    return redacted_url(text) if _URL_ARGUMENT_RE.match(text) else value


def _default_http_client(headers: Dict[str, str]) -> Any:
    import httpx2

    return httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(HTTP_TIMEOUT_SECONDS, read=HTTP_READ_TIMEOUT_SECONDS),
    )


async def _list_all_tools(client: Any) -> List[McpToolInfo]:
    tools: List[McpToolInfo] = []
    cursor: Optional[str] = None
    for _ in range(_MAX_LIST_PAGES):
        page = await client.list_tools(cursor=cursor)
        tools.extend(
            McpToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema or {}),
            )
            for tool in page.tools
        )
        cursor = page.next_cursor
        if not cursor:
            return tools
    raise McpCallError(f"tools/list did not finish within {_MAX_LIST_PAGES} pages")


def describe_exception(exc: BaseException) -> str:
    """The first leaf of an exception group, as `Type: message`."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip()
    code = getattr(exc, "code", None)
    label = type(exc).__name__ + (f" {code}" if isinstance(code, int) else "")
    return f"{label}: {text}" if text else label
