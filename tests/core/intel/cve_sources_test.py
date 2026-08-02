"""Tests for the CVE sources — payload mapping, KEV caching, and fan-out."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

import httpx

from kerkov.core.intel.cache import IntelCache
from kerkov.core.intel.cve_sources import (
    EPSS_API_URL,
    KEV_NAMESPACE,
    NVD_API_URL,
    CveSource,
    EpssSource,
    KevSource,
    NvdSource,
    gather_records,
)
from kerkov.core.intel.models import SourceRecord

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _build_mock_async_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    """Return a factory wrapping the real AsyncClient with a MockTransport."""

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return factory


def _patch_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Any:
    return patch(
        "kerkov.core.intel.cve_sources.httpx.AsyncClient",
        _build_mock_async_client(handler),
    )


def _create_mock_nvd_payload() -> Dict[str, Any]:
    return {
        "vulnerabilities": [
            {
                "cve": {
                    "id": "CVE-2021-44228",
                    "descriptions": [
                        {"lang": "es", "value": "descripcion"},
                        {"lang": "en", "value": "Apache Log4j2 JNDI features..."},
                    ],
                    "metrics": {
                        "cvssMetricV31": [
                            {
                                "cvssData": {
                                    "baseScore": 10.0,
                                    "baseSeverity": "CRITICAL",
                                    "vectorString": "CVSS:3.1/AV:N/AC:L",
                                }
                            }
                        ]
                    },
                    "weaknesses": [
                        {"description": [{"lang": "en", "value": "CWE-502"}]},
                        {"description": [{"lang": "en", "value": "NVD-CWE-noinfo"}]},
                    ],
                    "references": [
                        {"url": "https://example.test/a"},
                        {"url": "https://example.test/b"},
                    ],
                }
            }
        ]
    }


def _create_mock_nvd_metrics_payload(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """An NVD response carrying only the given metrics block."""
    return {"vulnerabilities": [{"cve": {"id": "CVE-2024-0182", "metrics": metrics}}]}


def _create_mock_kev_payload() -> Dict[str, Any]:
    return {
        "vulnerabilities": [
            {
                "cveID": "CVE-2021-44228",
                "dateAdded": "2021-12-10",
                "dueDate": "2021-12-24",
            },
            {"cveID": "CVE-2017-0144", "dateAdded": "2022-03-25"},
        ]
    }


def _create_mock_epss_payload() -> Dict[str, Any]:
    return {
        "status": "OK",
        "data": [{"cve": "CVE-2021-44228", "epss": "0.97565", "percentile": "0.99998"}],
    }


def _fixed_now() -> datetime:
    return datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def _rendered_logs(mock_logger: Any) -> str:
    """Interpolate logged calls the way logging would, so args are checked too."""
    return " ".join(
        call.args[0] % call.args[1:] if len(call.args) > 1 else str(call.args[0])
        for call in mock_logger.info.call_args_list
    )


class NvdSourceTest(unittest.TestCase):
    def test_payload_maps_to_source_record(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).startswith(NVD_API_URL))
            self.assertEqual(request.url.params.get("cveId"), "CVE-2021-44228")
            return httpx.Response(200, json=_create_mock_nvd_payload())

        source = NvdSource(timeout_seconds=15)
        with _patch_client(handler):
            record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertEqual(record.source, "nvd")
        self.assertEqual(record.cve_id, "CVE-2021-44228")
        self.assertEqual(record.cvss_score, 10.0)
        self.assertEqual(record.cvss_severity, "CRITICAL")
        self.assertEqual(record.cvss_vector, "CVSS:3.1/AV:N/AC:L")
        self.assertEqual(record.cwe_ids, ["CWE-502"])
        self.assertEqual(len(record.references), 2)
        self.assertTrue(record.title is not None and record.title.startswith("Apache"))

    def test_v40_metric_wins_over_v31(self) -> None:
        payload = _create_mock_nvd_payload()
        payload["vulnerabilities"][0]["cve"]["metrics"]["cvssMetricV40"] = [
            {
                "cvssData": {
                    "baseScore": 9.3,
                    "baseSeverity": "CRITICAL",
                    "vectorString": "CVSS:4.0/AV:N",
                }
            }
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        source = NvdSource(timeout_seconds=15)
        with _patch_client(handler):
            record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertEqual(record.cvss_score, 9.3)
        self.assertEqual(record.cvss_vector, "CVSS:4.0/AV:N")

    def test_api_key_is_sent_as_header(self) -> None:
        seen: Dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, json=_create_mock_nvd_payload())

        source = NvdSource(timeout_seconds=15, api_key="SECRETKEY123")
        with _patch_client(handler):
            _run(source.fetch("CVE-2021-44228"))

        self.assertEqual(seen.get("apikey"), "SECRETKEY123")

    def test_api_key_value_is_never_logged(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_nvd_payload())

        source = NvdSource(timeout_seconds=15, api_key="SECRETKEY123")
        with _patch_client(handler):
            with patch("kerkov.core.intel.cve_sources.logger") as mock_logger:
                _run(source.fetch("CVE-2021-44228"))

        logged = _rendered_logs(mock_logger)
        self.assertNotIn("SECRETKEY123", logged)
        self.assertIn("usedApiKey=True", logged)

    def test_keyless_fetch_is_logged_as_unauthenticated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_nvd_payload())

        source = NvdSource(timeout_seconds=15)
        with _patch_client(handler):
            with patch("kerkov.core.intel.cve_sources.logger") as mock_logger:
                _run(source.fetch("CVE-2021-44228"))

        self.assertIn("usedApiKey=False", _rendered_logs(mock_logger))

    def test_unknown_cve_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"vulnerabilities": []})

        source = NvdSource(timeout_seconds=15)
        with _patch_client(handler):
            self.assertIsNone(_run(source.fetch("CVE-0000-0000")))

    def test_search_returns_empty_until_pr4(self) -> None:
        source = NvdSource(timeout_seconds=15)
        self.assertEqual(_run(source.search("log4j", 10)), [])


class NvdCvssSelectionTest(unittest.TestCase):
    """The metric list is unordered, so the primary assessment must be chosen."""

    def _fetch_cvss(self, metrics: Dict[str, Any]) -> SourceRecord:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_nvd_metrics_payload(metrics))

        source = NvdSource(timeout_seconds=15)
        with _patch_client(handler):
            record = _run(source.fetch("CVE-2024-0182"))

        assert record is not None
        return record

    def test_primary_assessment_wins_over_a_preceding_secondary(self) -> None:
        # The real CVE-2024-0182 ordering: a CNA's 7.3 ahead of NVD's 9.8.
        record = self._fetch_cvss(
            {
                "cvssMetricV31": [
                    {
                        "source": "cna@vuldb.com",
                        "type": "Secondary",
                        "cvssData": {
                            "baseScore": 7.3,
                            "baseSeverity": "HIGH",
                            "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U",
                        },
                    },
                    {
                        "source": "nvd@nist.gov",
                        "type": "Primary",
                        "cvssData": {
                            "baseScore": 9.8,
                            "baseSeverity": "CRITICAL",
                            "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C",
                        },
                    },
                ]
            }
        )

        self.assertEqual(record.cvss_score, 9.8)
        self.assertEqual(record.cvss_severity, "CRITICAL")
        self.assertIn("S:C", record.cvss_vector or "")

    def test_nvd_source_identifies_the_primary_without_a_type(self) -> None:
        record = self._fetch_cvss(
            {
                "cvssMetricV31": [
                    {
                        "source": "cna@vuldb.com",
                        "cvssData": {"baseScore": 7.3, "baseSeverity": "HIGH"},
                    },
                    {
                        "source": "nvd@nist.gov",
                        "cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"},
                    },
                ]
            }
        )

        self.assertEqual(record.cvss_score, 9.8)

    def test_first_valid_assessment_used_when_none_is_primary(self) -> None:
        record = self._fetch_cvss(
            {
                "cvssMetricV31": [
                    {
                        "source": "cna@vuldb.com",
                        "type": "Secondary",
                        "cvssData": {"baseScore": 7.3, "baseSeverity": "HIGH"},
                    }
                ]
            }
        )

        self.assertEqual(record.cvss_score, 7.3)
        self.assertEqual(record.cvss_severity, "HIGH")

    def test_scoreless_entry_is_skipped(self) -> None:
        record = self._fetch_cvss(
            {
                "cvssMetricV31": [
                    {"type": "Secondary", "cvssData": {"vectorString": "no score"}},
                    {
                        "source": "cna@vuldb.com",
                        "cvssData": {"baseScore": 6.1, "baseSeverity": "MEDIUM"},
                    },
                ]
            }
        )

        self.assertEqual(record.cvss_score, 6.1)

    def test_newer_metric_version_outranks_older(self) -> None:
        record = self._fetch_cvss(
            {
                "cvssMetricV2": [
                    {"cvssData": {"baseScore": 7.5}, "baseSeverity": "HIGH"}
                ],
                "cvssMetricV31": [
                    {
                        "source": "nvd@nist.gov",
                        "type": "Primary",
                        "cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"},
                    }
                ],
            }
        )

        self.assertEqual(record.cvss_score, 9.8)

    def test_cvss_v2_is_used_when_it_is_the_only_scoring(self) -> None:
        # v2 carries baseSeverity beside cvssData rather than inside it.
        record = self._fetch_cvss(
            {
                "cvssMetricV2": [
                    {
                        "source": "nvd@nist.gov",
                        "type": "Primary",
                        "cvssData": {
                            "baseScore": 7.5,
                            "vectorString": "AV:N/AC:L/Au:N/C:P/I:P/A:P",
                        },
                        "baseSeverity": "HIGH",
                    }
                ]
            }
        )

        self.assertEqual(record.cvss_score, 7.5)
        self.assertEqual(record.cvss_severity, "HIGH")
        self.assertEqual(record.cvss_vector, "AV:N/AC:L/Au:N/C:P/I:P/A:P")

    def test_no_usable_metrics_yields_no_cvss(self) -> None:
        record = self._fetch_cvss({"ssvcV203": [{"type": None}]})

        self.assertIsNone(record.cvss_score)
        self.assertIsNone(record.cvss_severity)


class KevSourceTest(unittest.TestCase):
    def test_membership_hit_sets_known_exploited_and_dates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_kev_payload())

        with tempfile.TemporaryDirectory() as tmp:
            source = KevSource(
                timeout_seconds=15,
                cache=IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now),
                now_fn=_fixed_now,
            )
            with _patch_client(handler):
                record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertEqual(record.source, "kev")
        self.assertTrue(record.known_exploited)
        self.assertEqual(record.kev_date_added, "2021-12-10")
        self.assertEqual(record.kev_due_date, "2021-12-24")

    def test_membership_miss_is_known_exploited_false_not_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_kev_payload())

        with tempfile.TemporaryDirectory() as tmp:
            source = KevSource(
                timeout_seconds=15,
                cache=IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now),
                now_fn=_fixed_now,
            )
            with _patch_client(handler):
                record = _run(source.fetch("CVE-2024-99999"))

        assert record is not None
        self.assertIs(record.known_exploited, False)
        self.assertIsNone(record.kev_date_added)

    def test_feed_is_fetched_once_per_day_for_many_cves(self) -> None:
        calls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=_create_mock_kev_payload())

        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now)
            source = KevSource(timeout_seconds=15, cache=cache, now_fn=_fixed_now)
            with _patch_client(handler):
                _run(source.fetch("CVE-2021-44228"))
                _run(source.fetch("CVE-2017-0144"))
                _run(source.fetch("CVE-2024-99999"))

            self.assertEqual(len(calls), 1)
            self.assertIsNotNone(cache.get(KEV_NAMESPACE, "2026-07-25"))

    def test_cached_snapshot_is_reused_by_a_fresh_source(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network must not be touched on a cache hit")

        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now)
            cache.put(
                KEV_NAMESPACE,
                "2026-07-25",
                {"CVE-2021-44228": {"date_added": "2021-12-10", "due_date": None}},
            )
            source = KevSource(timeout_seconds=15, cache=cache, now_fn=_fixed_now)
            with _patch_client(handler):
                record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertTrue(record.known_exploited)

    def test_cached_snapshot_reports_when_it_was_fetched(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network must not be touched on a cache hit")

        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now)
            cache.put(KEV_NAMESPACE, "2026-07-25", {"CVE-2021-44228": {}})
            source = KevSource(timeout_seconds=15, cache=cache, now_fn=_fixed_now)
            with _patch_client(handler):
                record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertEqual(record.fetched_at, _fixed_now().isoformat())

    def test_live_fetch_leaves_fetched_at_unset(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_kev_payload())

        with tempfile.TemporaryDirectory() as tmp:
            source = KevSource(
                timeout_seconds=15,
                cache=IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now),
                now_fn=_fixed_now,
            )
            with _patch_client(handler):
                record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertIsNone(record.fetched_at, "a live fetch is not a snapshot")

    def test_refresh_bypasses_the_daily_snapshot(self) -> None:
        calls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=_create_mock_kev_payload())

        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now)
            cache.put(KEV_NAMESPACE, "2026-07-25", {})
            source = KevSource(timeout_seconds=15, cache=cache, now_fn=_fixed_now)
            with _patch_client(handler):
                record = _run(source.fetch("CVE-2021-44228", refresh=True))

        self.assertEqual(len(calls), 1, "refresh must re-fetch the feed")
        assert record is not None
        self.assertTrue(record.known_exploited, "the stale empty snapshot was used")

    def test_cache_write_failure_does_not_lose_the_feed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_create_mock_kev_payload())

        with tempfile.TemporaryDirectory() as tmp:
            cache = IntelCache(tmp, ttl_hours=24, now_fn=_fixed_now)
            source = KevSource(timeout_seconds=15, cache=cache, now_fn=_fixed_now)
            with (
                _patch_client(handler),
                patch.object(
                    cache, "put", side_effect=OSError("read-only file system")
                ),
            ):
                record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertTrue(record.known_exploited)


class EpssSourceTest(unittest.TestCase):
    def test_string_scores_are_coerced_to_floats(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).startswith(EPSS_API_URL))
            return httpx.Response(200, json=_create_mock_epss_payload())

        source = EpssSource(timeout_seconds=15)
        with _patch_client(handler):
            record = _run(source.fetch("CVE-2021-44228"))

        assert record is not None
        self.assertEqual(record.source, "epss")
        self.assertAlmostEqual(record.epss_score or 0.0, 0.97565)
        self.assertAlmostEqual(record.epss_percentile or 0.0, 0.99998)

    def test_empty_data_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "OK", "data": []})

        source = EpssSource(timeout_seconds=15)
        with _patch_client(handler):
            self.assertIsNone(_run(source.fetch("CVE-0000-0000")))


class _StubSource(CveSource):
    def __init__(
        self,
        name: str,
        record: Optional[SourceRecord] = None,
        error: Optional[Exception] = None,
    ) -> None:
        super().__init__(name, timeout_seconds=1)
        self._record = record
        self._error = error
        self.refresh_calls: List[bool] = []

    async def fetch(self, cve_id: str, refresh: bool = False) -> Optional[SourceRecord]:
        self.refresh_calls.append(refresh)
        if self._error is not None:
            raise self._error
        return self._record


class GatherRecordsTest(unittest.TestCase):
    def test_failing_source_is_recorded_and_others_still_return(self) -> None:
        good = _StubSource(
            "nvd", SourceRecord(source="nvd", cve_id="CVE-1", cvss_score=10.0)
        )
        broken = _StubSource("epss", error=httpx.TimeoutException("slow"))
        empty = _StubSource("kev", None)

        records, errors = _run(gather_records([good, broken, empty], "CVE-1"))

        self.assertEqual([r.source for r in records], ["nvd"])
        self.assertEqual(errors, {"epss": "timed out"})

    def test_http_status_error_is_described_by_code(self) -> None:
        response = httpx.Response(503, request=httpx.Request("GET", "http://x"))
        broken = _StubSource(
            "nvd",
            error=httpx.HTTPStatusError(
                "boom", request=response.request, response=response
            ),
        )

        records, errors = _run(gather_records([broken], "CVE-1"))

        self.assertEqual(records, [])
        self.assertEqual(errors, {"nvd": "HTTP 503"})

    def test_all_sources_failing_is_not_fatal(self) -> None:
        sources: List[CveSource] = [
            _StubSource("nvd", error=RuntimeError("a")),
            _StubSource("epss", error=RuntimeError("b")),
        ]

        records, errors = _run(gather_records(sources, "CVE-1"))

        self.assertEqual(records, [])
        self.assertEqual(set(errors), {"nvd", "epss"})

    def test_refresh_is_forwarded_to_every_source(self) -> None:
        sources = [_StubSource("nvd"), _StubSource("epss")]

        _run(gather_records(list(sources), "CVE-1", refresh=True))

        self.assertEqual([source.refresh_calls for source in sources], [[True], [True]])

    def test_refresh_defaults_to_false(self) -> None:
        source = _StubSource("nvd")

        _run(gather_records([source], "CVE-1"))

        self.assertEqual(source.refresh_calls, [False])


if __name__ == "__main__":
    unittest.main()
