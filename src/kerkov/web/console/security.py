"""Security middleware for the local web console."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional
from urllib.parse import SplitResult, urlsplit

ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApplication = Callable[
    [dict[str, Any], ASGIReceive, ASGISend],
    Awaitable[Any],
]

_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}
_ORIGIN_TO_WEBSOCKET_SCHEME = {
    "http": "ws",
    "https": "wss",
}


def _single_header(scope: dict[str, Any], name: bytes) -> Optional[str]:
    values: list[bytes] = []
    for header in scope.get("headers", []):
        if (
            not isinstance(header, (list, tuple))
            or len(header) != 2
            or not isinstance(header[0], bytes)
            or not isinstance(header[1], bytes)
        ):
            continue
        header_name, value = header
        if header_name.lower() == name:
            values.append(value)
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _port(parsed: SplitResult) -> Optional[int]:
    try:
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        return None
    return _DEFAULT_PORTS.get(parsed.scheme.lower())


def _origin_authority(origin: str) -> Optional[tuple[str, str, int]]:
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
    except ValueError:
        return None
    origin_scheme = parsed.scheme.lower()
    websocket_scheme = _ORIGIN_TO_WEBSOCKET_SCHEME.get(origin_scheme)
    port = _port(parsed)
    if (
        websocket_scheme is None
        or hostname is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return websocket_scheme, hostname.lower(), port


def _endpoint_authority(scope: dict[str, Any]) -> Optional[tuple[str, str, int]]:
    # Daphne omits ``scheme`` from its WebSocket scope; direct connections to
    # its development server are plain WebSockets unless another server says
    # otherwise.
    scheme = str(scope.get("scheme") or "ws").lower()
    host = _single_header(scope, b"host")
    if scheme not in {"ws", "wss"} or host is None:
        return None

    try:
        parsed = urlsplit(f"{scheme}://{host}")
        hostname = parsed.hostname
    except ValueError:
        return None
    port = _port(parsed)
    if (
        hostname is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return scheme, hostname.lower(), port


def has_same_origin_authority(scope: dict[str, Any]) -> bool:
    """Match browser origin and WebSocket endpoint by scheme, host, and port."""

    origin = _single_header(scope, b"origin")
    if origin is None:
        return False
    return _origin_authority(origin) == _endpoint_authority(scope)


class SameOriginWebSocketValidator:
    """Reject WebSockets not opened by the exact HTTP endpoint origin."""

    def __init__(self, application: ASGIApplication) -> None:
        self.application = application

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> Any:
        if scope.get("type") != "websocket":
            raise ValueError(
                "SameOriginWebSocketValidator can wrap only WebSocket applications."
            )
        if not has_same_origin_authority(scope):
            await send({"type": "websocket.close", "code": 1008})
            return None
        return await self.application(scope, receive, send)
