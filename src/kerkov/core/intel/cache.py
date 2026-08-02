import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from kerkov.core.json_state import load_json_file, write_json_file

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize(component: str) -> str:
    cleaned = _UNSAFE_KEY_CHARS.sub("_", component.strip())
    return cleaned if cleaned.strip(".") else "_"


def _resolve_base(base_dir: str) -> Path:
    path = Path(base_dir).expanduser()
    return path if path.is_absolute() else Path.home() / path


class IntelCache:
    """TTL disk cache for intel records, homed under the user's home dir.

    One entry is one JSON file at ``<base>/<namespace>/<key>.json`` holding
    ``{"fetched_at": <iso8601>, "payload": {...}}``. Reads honor the TTL: a
    stale, missing, or malformed entry returns ``None`` so the caller re-fetches.

    ``base_dir`` is expanded with ``~`` semantics (the cache lives in
    ``~/.kerkov/cache/intel`` by default). An absolute override is used verbatim;
    a relative one is anchored to the user's home, so the cache never lands in
    the checkout or in whatever directory Kerkov happened to be launched from.
    """

    def __init__(
        self,
        base_dir: str,
        ttl_hours: int,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._base = _resolve_base(base_dir)
        self._ttl = timedelta(hours=ttl_hours)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def _path(self, namespace: str, key: str) -> Path:
        return self._base / _sanitize(namespace) / f"{_sanitize(key)}.json"

    def get_entry(
        self, namespace: str, key: str
    ) -> Optional[Tuple[Dict[str, Any], datetime]]:
        """Return the payload *and* when it was fetched, or None on miss.

        Callers that report how old a cached fact is need the timestamp; the
        cache already stores it, so exposing it here beats making them stamp a
        second copy into the payload.
        """
        entry = load_json_file(self._path(namespace, key))
        if not entry:
            return None

        fetched_raw = entry.get("fetched_at")
        payload = entry.get("payload")
        if not isinstance(fetched_raw, str) or not isinstance(payload, dict):
            return None

        try:
            fetched_at = datetime.fromisoformat(fetched_raw)
        except ValueError:
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)

        if self._now() - fetched_at >= self._ttl:
            return None
        return payload, fetched_at

    def get(self, namespace: str, key: str) -> Optional[Dict[str, Any]]:
        found = self.get_entry(namespace, key)
        return None if found is None else found[0]

    def put(self, namespace: str, key: str, payload: Dict[str, Any]) -> None:
        entry = {"fetched_at": self._now().isoformat(), "payload": payload}
        write_json_file(self._path(namespace, key), entry)
