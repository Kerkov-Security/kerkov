"""Tests for CveLookupTool — the CVE intelligence lookup tool."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from unittest.mock import patch

import httpx

from kerkov.core.config_manager import ConfigManager
from kerkov.core.intel.cache import IntelCache
from kerkov.core.intel.cve_sources import CveSource
from kerkov.core.intel.models import SourceRecord
from kerkov.tools.cve_lookup_tool import CveLookupTool

CVE = "CVE-2021-44228"

FIXED_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _create_mock_config(
    cache_dir: str,
    *,
    nvd_api_key: Optional[str] = None,
    ttl_hours: int = 24,
) -> ConfigManager:
    return ConfigManager(
        intel_cache_dir=cache_dir,
        cve_cache_ttl_hours=ttl_hours,
        cve_lookup_timeout_seconds=5,
        nvd_api_key=nvd_api_key,
    )


def _create_mock_nvd_record() -> SourceRecord:
    return SourceRecord(
        source="nvd",
        cve_id=CVE,
        title="Apache Log4j2 JNDI features do not protect against attacker "
        "controlled LDAP endpoints.",
        cvss_score=10.0,
        cvss_severity="CRITICAL",
        cvss_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H",
        cwe_ids=["CWE-502", "CWE-400"],
        references=[f"https://example.test/ref{i}" for i in range(8)],
        raw={"secret_payload": "must never be surfaced"},
    )


def _create_mock_kev_record(known_exploited: bool = True) -> SourceRecord:
    return SourceRecord(
        source="kev",
        cve_id=CVE,
        known_exploited=known_exploited,
        kev_date_added="2021-12-10" if known_exploited else None,
        kev_due_date="2021-12-24" if known_exploited else None,
    )


def _create_mock_epss_record() -> SourceRecord:
    return SourceRecord(
        source="epss",
        cve_id=CVE,
        epss_score=0.97456,
        epss_percentile=0.99954,
    )


class _FakeSource(CveSource):
    """A source that returns a canned record, raises, or reports nothing."""

    def __init__(
        self,
        name: str,
        record: Optional[SourceRecord] = None,
        error: Optional[BaseException] = None,
        cacheable: bool = True,
    ) -> None:
        super().__init__(name, 5)
        self._record = record
        self._error = error
        self._cacheable = cacheable
        self.fetch_calls: List[str] = []

    @property
    def per_cve_cacheable(self) -> bool:
        return self._cacheable

    async def fetch(self, cve_id: str) -> Optional[SourceRecord]:
        self.fetch_calls.append(cve_id)
        if self._error is not None:
            raise self._error
        return self._record


class _CveLookupToolTestBase(unittest.TestCase):
    """Builds a tool against a throwaway cache dir and injectable sources."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = FIXED_NOW

    def _build_tool(
        self,
        sources: Optional[List[CveSource]] = None,
        *,
        nvd_api_key: Optional[str] = None,
        ttl_hours: int = 24,
    ) -> CveLookupTool:
        config = _create_mock_config(
            self._tmp.name, nvd_api_key=nvd_api_key, ttl_hours=ttl_hours
        )
        with patch("kerkov.tools.cve_lookup_tool.get_config", return_value=config):
            tool = CveLookupTool()

        # Shared cache dir with an injected clock, so TTL expiry is testable
        # without sleeping.
        tool._cache = IntelCache(
            base_dir=self._tmp.name,
            ttl_hours=ttl_hours,
            now_fn=lambda: self.now,
        )
        if sources is not None:
            tool._sources = sources
        return tool

    def _default_sources(self) -> List[_FakeSource]:
        return [
            _FakeSource("nvd", _create_mock_nvd_record()),
            _FakeSource("kev", _create_mock_kev_record(), cacheable=False),
            _FakeSource("epss", _create_mock_epss_record()),
        ]


class ParamValidationTest(_CveLookupToolTestBase):
    def test_missing_cve_id_rejected(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({}))
        self.assertFalse(result.success)
        self.assertIn("cve_id", result.error or "")

    def test_blank_cve_id_rejected(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({"cve_id": "   "}))
        self.assertFalse(result.success)
        self.assertIn("cve_id", result.error or "")

    def test_malformed_cve_id_rejected(self) -> None:
        tool = self._build_tool(self._default_sources())
        for bad in ("CVE-21-44228", "log4shell", "CVE-2021", "2021-44228"):
            with self.subTest(cve_id=bad):
                result = _run(tool.execute({"cve_id": bad}))
                self.assertFalse(result.success)
                self.assertIn("cve_id", result.error or "")

    def test_lowercase_cve_id_normalized(self) -> None:
        sources = self._default_sources()
        tool = self._build_tool(sources)
        result = _run(tool.execute({"cve_id": "cve-2021-44228"}))

        self.assertTrue(result.success)
        self.assertEqual(sources[0].fetch_calls, [CVE])
        self.assertEqual(result.metadata["cve_id"], CVE)

    def test_unknown_action_rejected(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({"action": "search", "cve_id": CVE}))
        self.assertFalse(result.success)


class FormatTest(_CveLookupToolTestBase):
    def test_merged_summary_reports_every_signal(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({"cve_id": CVE}))

        self.assertTrue(result.success)
        self.assertIn(CVE, result.output)
        self.assertIn("CVSS 10.0 CRITICAL", result.output)
        self.assertIn("CWE-502", result.output)
        self.assertIn("KNOWN EXPLOITED IN THE WILD", result.output)
        self.assertIn("added 2021-12-10", result.output)
        self.assertIn("0.97456", result.output)
        self.assertIn("epss, kev, nvd", result.output)

    def test_kev_miss_reported_as_not_listed(self) -> None:
        sources = [
            _FakeSource("nvd", _create_mock_nvd_record()),
            _FakeSource("kev", _create_mock_kev_record(False), cacheable=False),
        ]
        tool = self._build_tool(sources)
        result = _run(tool.execute({"cve_id": CVE}))

        self.assertTrue(result.success)
        self.assertIn("not listed in CISA KEV", result.output)
        self.assertIs(result.metadata["record"]["known_exploited"], False)

    def test_references_capped_in_output_but_complete_in_metadata(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({"cve_id": CVE}))

        self.assertIn("showing 5 of 8", result.output)
        self.assertIn("https://example.test/ref4", result.output)
        self.assertNotIn("https://example.test/ref5", result.output)
        self.assertEqual(len(result.metadata["record"]["references"]), 8)

    def test_raw_source_payload_never_surfaced(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({"cve_id": CVE}))

        self.assertNotIn("must never be surfaced", result.output)
        self.assertNotIn("must never be surfaced", json.dumps(result.metadata))

    def test_provenance_included_in_metadata(self) -> None:
        tool = self._build_tool(self._default_sources())
        result = _run(tool.execute({"cve_id": CVE}))

        provenance = result.metadata["record"]["provenance"]
        self.assertEqual(provenance["cvss_score"], "nvd")
        self.assertEqual(provenance["known_exploited"], "kev")
        self.assertEqual(provenance["epss_score"], "epss")


class DegradedSourceTest(_CveLookupToolTestBase):
    def test_one_failing_source_does_not_fail_the_lookup(self) -> None:
        sources = [
            _FakeSource("nvd", _create_mock_nvd_record()),
            _FakeSource("kev", error=httpx.TimeoutException("slow"), cacheable=False),
            _FakeSource("epss", _create_mock_epss_record()),
        ]
        tool = self._build_tool(sources)
        result = _run(tool.execute({"cve_id": CVE}))

        self.assertTrue(result.success)
        self.assertIn("CVSS 10.0", result.output)
        self.assertIn("Unavailable", result.output)
        self.assertEqual(result.metadata["record"]["source_errors"]["kev"], "timed out")

    def test_all_sources_failing_reports_failure(self) -> None:
        sources = [
            _FakeSource("nvd", error=httpx.TimeoutException("slow")),
            _FakeSource("epss", error=httpx.HTTPError("boom")),
        ]
        tool = self._build_tool(sources)
        result = _run(tool.execute({"cve_id": CVE}))

        self.assertFalse(result.success)
        self.assertIn("nvd: timed out", result.error or "")
        self.assertIn("epss", result.error or "")

    def test_unknown_cve_reports_no_data_without_failing(self) -> None:
        sources = [
            _FakeSource("nvd", None),
            _FakeSource("kev", _create_mock_kev_record(False), cacheable=False),
            _FakeSource("epss", None),
        ]
        tool = self._build_tool(sources)
        result = _run(tool.execute({"cve_id": "CVE-2099-99999"}))

        self.assertTrue(result.success)
        self.assertIn("No data found", result.output)
        self.assertNotIn("not listed in CISA KEV", result.output)


class CacheTest(_CveLookupToolTestBase):
    def test_second_lookup_served_from_cache(self) -> None:
        first = self._default_sources()
        _run(self._build_tool(first).execute({"cve_id": CVE}))

        second = self._default_sources()
        result = _run(self._build_tool(second).execute({"cve_id": CVE}))

        self.assertTrue(result.success)
        self.assertTrue(result.metadata["cache_hit"])
        self.assertIn("from cache", result.output)
        self.assertEqual(second[0].fetch_calls, [], "nvd should not be re-fetched")
        self.assertEqual(second[2].fetch_calls, [], "epss should not be re-fetched")
        self.assertIn("CVSS 10.0 CRITICAL", result.output)

    def test_non_cacheable_source_always_fetched(self) -> None:
        _run(self._build_tool(self._default_sources()).execute({"cve_id": CVE}))

        second = self._default_sources()
        _run(self._build_tool(second).execute({"cve_id": CVE}))

        self.assertEqual(second[1].fetch_calls, [CVE], "kev owns its own cache")

    def test_refresh_bypasses_the_cache(self) -> None:
        _run(self._build_tool(self._default_sources()).execute({"cve_id": CVE}))

        second = self._default_sources()
        result = _run(
            self._build_tool(second).execute({"cve_id": CVE, "refresh": True})
        )

        self.assertEqual(second[0].fetch_calls, [CVE])
        self.assertFalse(result.metadata["cache_hit"])
        self.assertIn("fetched live", result.output)

    def test_expired_entry_is_refetched(self) -> None:
        _run(self._build_tool(self._default_sources()).execute({"cve_id": CVE}))

        self.now = FIXED_NOW + timedelta(hours=25)
        second = self._default_sources()
        result = _run(self._build_tool(second).execute({"cve_id": CVE}))

        self.assertEqual(second[0].fetch_calls, [CVE])
        self.assertFalse(result.metadata["cache_hit"])

    def test_fetched_at_reports_the_cached_fetch_time(self) -> None:
        _run(self._build_tool(self._default_sources()).execute({"cve_id": CVE}))

        self.now = FIXED_NOW + timedelta(hours=6)
        result = _run(
            self._build_tool(self._default_sources()).execute({"cve_id": CVE})
        )

        self.assertEqual(result.metadata["record"]["fetched_at"], FIXED_NOW.isoformat())

    def test_malformed_cache_entry_falls_back_to_the_source(self) -> None:
        tool = self._build_tool(self._default_sources())
        tool._cache.put("nvd", CVE, {"cve_id": 42})

        sources = self._default_sources()
        result = _run(self._build_tool(sources).execute({"cve_id": CVE}))

        self.assertTrue(result.success)
        self.assertEqual(sources[0].fetch_calls, [CVE])
        self.assertIn("CVSS 10.0", result.output)


class SecretRedactionTest(_CveLookupToolTestBase):
    def test_nvd_api_key_never_in_output_metadata_or_logs(self) -> None:
        key = "nvd-SECRETKEY123"
        tool = self._build_tool(self._default_sources(), nvd_api_key=key)

        with self.assertLogs("Tool.cve_lookup", level="INFO") as logs:
            result = _run(tool.execute({"cve_id": CVE}))

        self.assertTrue(result.success)
        self.assertNotIn(key, result.output)
        self.assertNotIn(key, json.dumps(result.metadata))
        self.assertFalse(any(key in line for line in logs.output))


class RegistrationTest(unittest.TestCase):
    def test_cve_lookup_registered_unconditionally(self) -> None:
        from kerkov.tools.tool_suite import ToolSuite

        names = [tool.name for tool in ToolSuite.get_tools()]
        self.assertIn("cve_lookup", names)


if __name__ == "__main__":
    unittest.main()
