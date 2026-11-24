"""Load provider model catalogs and explain why one could not be loaded."""

import errno
from http import HTTPStatus
from typing import Any, Callable, List, Optional, Set
from urllib.parse import urlsplit

import httpx

_REFUSED_ERRNOS = {errno.ECONNREFUSED, 10061}  # 10061 is Windows WSAECONNREFUSED.


class ModelCatalogError(Exception):
    """A model catalog failed to load; the message is safe to show the operator.

    Messages name the provider, its scheme/host/port and the cause only. They
    never carry a credential, request header, URL path or response body.
    """


def fetch_model_ids(
    label: str,
    base_url: str,
    fetch: Callable[[], httpx.Response],
    list_key: str,
    id_key: str,
) -> List[str]:
    """Fetch a catalog with `fetch` and return the ids under `list_key`/`id_key`."""
    payload = _fetch_payload(label, base_url, fetch)
    entries = payload.get(list_key) if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise _unreadable(label, base_url)
    return [
        entry[id_key]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get(id_key), str)
        and entry[id_key]
    ]


def display_endpoint(base_url: str) -> str:
    """Return `scheme://host[:port]`, dropping userinfo, path, query and fragment."""
    try:
        parts = urlsplit(base_url)
    except ValueError:
        return "the configured endpoint"
    host = parts.netloc.rpartition("@")[2]
    if not parts.scheme or not host:
        return "the configured endpoint"
    return f"{parts.scheme}://{host}"


def _fetch_payload(
    label: str, base_url: str, fetch: Callable[[], httpx.Response]
) -> Any:
    endpoint = display_endpoint(base_url)
    # `from None` keeps the transport exception, whose text can hold the full
    # URL, out of any traceback logged further up.
    try:
        response = fetch()
    except httpx.TimeoutException:
        raise ModelCatalogError(
            f"Couldn't reach {label} at {endpoint} (timed out)."
        ) from None
    except httpx.TransportError as exc:
        reason = "connection refused" if _refused(exc) else "connection failed"
        raise ModelCatalogError(
            f"Couldn't reach {label} at {endpoint} ({reason})."
        ) from None
    if response.status_code >= 400:
        raise ModelCatalogError(
            f"{label} at {endpoint} returned {_status(response.status_code)} "
            "for its model list."
        )
    try:
        return response.json()
    except ValueError:
        raise _unreadable(label, base_url) from None


def _unreadable(label: str, base_url: str) -> ModelCatalogError:
    return ModelCatalogError(
        f"{label} at {display_endpoint(base_url)} returned an unreadable model list."
    )


def _status(code: int) -> str:
    try:
        return f"HTTP {code} {HTTPStatus(code).phrase}"
    except ValueError:
        return f"HTTP {code}"


def _refused(exc: BaseException) -> bool:
    seen: Set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError) or (
            getattr(current, "errno", None) in _REFUSED_ERRNOS
        ):
            return True
        current = current.__cause__ or current.__context__
    return False
