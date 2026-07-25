"""Tests for IntelCache — the TTL disk cache under ~/.kerkov."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kerkov.core.intel.cache import IntelCache


class _Clock:
    """A movable clock so TTL expiry is tested without real sleeps."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def _fixed_start() -> datetime:
    return datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


class IntelCacheTest(unittest.TestCase):
    def test_put_then_get_within_ttl_returns_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(_fixed_start())
            cache = IntelCache(tmp, ttl_hours=24, now_fn=clock)

            cache.put("nvd", "CVE-2021-44228", {"cvss_score": 10.0})
            clock.advance(hours=23)

            self.assertEqual(cache.get("nvd", "CVE-2021-44228"), {"cvss_score": 10.0})

    def test_get_missing_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_Clock(_fixed_start()))
            self.assertIsNone(cache.get("nvd", "CVE-0000-0000"))

    def test_entry_past_ttl_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(_fixed_start())
            cache = IntelCache(tmp, ttl_hours=24, now_fn=clock)

            cache.put("epss", "CVE-2021-44228", {"epss_score": 0.9})
            clock.advance(hours=24, minutes=1)

            self.assertIsNone(cache.get("epss", "CVE-2021-44228"))

    def test_put_overwrites_and_refreshes_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock(_fixed_start())
            cache = IntelCache(tmp, ttl_hours=24, now_fn=clock)

            cache.put("nvd", "CVE-2021-44228", {"cvss_score": 9.8})
            clock.advance(hours=23)
            cache.put("nvd", "CVE-2021-44228", {"cvss_score": 10.0})
            clock.advance(hours=23)

            self.assertEqual(cache.get("nvd", "CVE-2021-44228"), {"cvss_score": 10.0})

    def test_malformed_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_Clock(_fixed_start()))
            path = Path(tmp) / "nvd" / "CVE-2021-44228.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"payload": {"x": 1}}', encoding="utf-8")

            self.assertIsNone(cache.get("nvd", "CVE-2021-44228"))

    def test_unsafe_key_is_sanitized_into_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_Clock(_fixed_start()))
            cache.put("kev", "../../etc/passwd", {"x": 1})

            files = list((Path(tmp) / "kev").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertNotIn("/", files[0].name)
            self.assertNotIn("\\", files[0].name)

    def test_tilde_resolves_under_home_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
                cache = IntelCache(
                    "~/.kerkov/cache/intel",
                    ttl_hours=24,
                    now_fn=_Clock(_fixed_start()),
                )
                cache.put("nvd", "CVE-2021-44228", {"cvss_score": 10.0})

            expected = (
                Path(home)
                / ".kerkov"
                / "cache"
                / "intel"
                / "nvd"
                / "CVE-2021-44228.json"
            )
            self.assertTrue(expected.exists())


if __name__ == "__main__":
    unittest.main()
