"""One long-lived connection to one MCP server.

The SDK's `Client` is strictly `async with`, and anyio requires a cancel scope to
be entered and exited in the same task, so a holder task owns that block for the
connection's lifetime. Requests do not need to run in that task: callers use the
entered client directly, raced against the holder so a dead server fails the call
instead of hanging it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Any, AsyncContextManager, Callable, Dict, List, Optional, Set

from kerkov.core.logging.logger_factory import DEFAULT_LOGGING_DIR, get_logger
from kerkov.core.mcp.config import STDIO, ResolvedServer

logger = get_logger("McpConnection")

CONNECT_TIMEOUT_SECONDS = 30.0
CLOSE_TIMEOUT_SECONDS = 10.0
_MAX_LIST_PAGES = 100

ClientFactory = Callable[[], AsyncContextManager[Any]]


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
    ) -> None:
        self.server = server
        self.key = server.config.key
        self.state = ConnectionState.NOT_STARTED
        self.error: Optional[str] = None
        self._client_factory = client_factory
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
            raise McpCallError(describe_exception(exc)) from exc

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
            self._fail(describe_exception(exc))
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
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if self.server.config.transport != STDIO:
            raise McpCallError(
                f"the {self.server.config.transport!r} transport is not supported yet"
            )
        params = StdioServerParameters(
            command=self.server.command or "",
            args=list(self.server.args),
            env=dict(self.server.env),
        )
        log_path = server_log_path(self.key)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._errlog = log_path.open("a", encoding="utf-8")
        return Client(stdio_client(params, errlog=self._errlog))

    def _fail(self, message: str) -> None:
        self.state = ConnectionState.FAILED
        self.error = message

    def _close_errlog(self) -> None:
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None


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
