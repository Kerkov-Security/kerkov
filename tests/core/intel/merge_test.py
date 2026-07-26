"""Tests for merge_records — field precedence, provenance, dedup, and capping."""

import unittest
from typing import Any

from kerkov.core.intel.merge import MAX_REFERENCES, merge_records
from kerkov.core.intel.models import SourceRecord


def _create_mock_nvd_record(**overrides: Any) -> SourceRecord:
    defaults = {
        "source": "nvd",
        "cve_id": "CVE-2021-44228",
        "title": "Apache Log4j2 JNDI",
        "cvss_score": 10.0,
        "cvss_severity": "CRITICAL",
        "cvss_vector": "CVSS:3.1/AV:N",
        "cwe_ids": ["CWE-502"],
        "references": ["https://example.test/nvd"],
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)


def _create_mock_circl_record(**overrides: Any) -> SourceRecord:
    defaults = {
        "source": "circl",
        "cve_id": "CVE-2021-44228",
        "title": "log4j RCE",
        "cvss_score": 9.8,
        "cvss_severity": "HIGH",
        "cvss_vector": "CVSS:3.0/AV:N",
        "cwe_ids": ["CWE-20"],
        "references": ["https://example.test/circl"],
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)


def _create_mock_kev_record(**overrides: Any) -> SourceRecord:
    defaults = {
        "source": "kev",
        "cve_id": "CVE-2021-44228",
        "known_exploited": True,
        "kev_date_added": "2021-12-10",
        "kev_due_date": "2021-12-24",
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)


def _create_mock_epss_record(**overrides: Any) -> SourceRecord:
    defaults = {
        "source": "epss",
        "cve_id": "CVE-2021-44228",
        "epss_score": 0.97565,
        "epss_percentile": 0.99998,
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)


class MergeRecordsTest(unittest.TestCase):
    def test_all_four_sources_fuse_into_one_answer(self) -> None:
        merged = merge_records(
            "CVE-2021-44228",
            [
                _create_mock_epss_record(),
                _create_mock_kev_record(),
                _create_mock_nvd_record(),
            ],
        )

        self.assertEqual(merged.cvss_score, 10.0)
        self.assertEqual(merged.cwe_ids, ["CWE-502"])
        self.assertIs(merged.known_exploited, True)
        self.assertAlmostEqual(merged.epss_score or 0.0, 0.97565)
        self.assertEqual(merged.source_errors, {})

    def test_nvd_wins_cvss_over_circl(self) -> None:
        merged = merge_records(
            "CVE-2021-44228",
            [_create_mock_circl_record(), _create_mock_nvd_record()],
        )

        self.assertEqual(merged.cvss_score, 10.0)
        self.assertEqual(merged.cvss_severity, "CRITICAL")
        self.assertEqual(merged.provenance["cvss_score"], "nvd")
        self.assertEqual(merged.provenance["title"], "nvd")

    def test_input_order_does_not_change_precedence(self) -> None:
        forward = merge_records(
            "CVE-1", [_create_mock_nvd_record(), _create_mock_circl_record()]
        )
        reverse = merge_records(
            "CVE-1", [_create_mock_circl_record(), _create_mock_nvd_record()]
        )

        self.assertEqual(forward.cvss_score, reverse.cvss_score)
        self.assertEqual(forward.provenance, reverse.provenance)

    def test_circl_fills_cvss_when_nvd_has_none(self) -> None:
        nvd = _create_mock_nvd_record(
            cvss_score=None, cvss_severity=None, cvss_vector=None
        )

        merged = merge_records("CVE-1", [nvd, _create_mock_circl_record()])

        self.assertEqual(merged.cvss_score, 9.8)
        self.assertEqual(merged.provenance["cvss_score"], "circl")
        self.assertEqual(merged.provenance["title"], "nvd")

    def test_cvss_triple_is_taken_from_a_single_source(self) -> None:
        nvd = _create_mock_nvd_record(cvss_vector=None)

        merged = merge_records("CVE-1", [nvd, _create_mock_circl_record()])

        self.assertEqual(merged.cvss_score, 10.0)
        self.assertIsNone(merged.cvss_vector)
        self.assertNotIn("cvss_vector", merged.provenance)

    def test_kev_and_epss_never_overwrite_cvss(self) -> None:
        rogue_kev = _create_mock_kev_record(cvss_score=1.0, cvss_severity="LOW")
        rogue_epss = _create_mock_epss_record(cvss_score=2.0)

        merged = merge_records(
            "CVE-1", [rogue_kev, rogue_epss, _create_mock_nvd_record()]
        )

        self.assertEqual(merged.cvss_score, 10.0)
        self.assertEqual(merged.provenance["cvss_score"], "nvd")

    def test_kev_false_is_preserved_not_treated_as_missing(self) -> None:
        merged = merge_records(
            "CVE-1",
            [
                _create_mock_nvd_record(),
                _create_mock_kev_record(
                    known_exploited=False, kev_date_added=None, kev_due_date=None
                ),
            ],
        )

        self.assertIs(merged.known_exploited, False)
        self.assertEqual(merged.provenance["known_exploited"], "kev")

    def test_references_are_deduped_and_capped(self) -> None:
        shared = "https://example.test/shared"
        nvd = _create_mock_nvd_record(
            references=[shared] + [f"https://example.test/{i}" for i in range(30)]
        )
        circl = _create_mock_circl_record(references=[shared])

        merged = merge_records("CVE-1", [nvd, circl])

        self.assertEqual(len(merged.references), MAX_REFERENCES)
        self.assertEqual(len(set(merged.references)), MAX_REFERENCES)
        self.assertEqual(merged.provenance["references"], "nvd")

    def test_cwe_ids_union_across_sources_without_duplicates(self) -> None:
        nvd = _create_mock_nvd_record(cwe_ids=["CWE-502", "CWE-20"])
        circl = _create_mock_circl_record(cwe_ids=["CWE-20", "CWE-400"])

        merged = merge_records("CVE-1", [nvd, circl])

        self.assertEqual(merged.cwe_ids, ["CWE-502", "CWE-20", "CWE-400"])

    def test_errors_are_folded_in_not_raised(self) -> None:
        merged = merge_records(
            "CVE-1",
            [_create_mock_nvd_record()],
            {"circl": "HTTP 503", "epss": "timed out"},
        )

        self.assertEqual(merged.cvss_score, 10.0)
        self.assertEqual(merged.source_errors["circl"], "HTTP 503")
        self.assertEqual(merged.source_errors["epss"], "timed out")

    def test_no_records_yields_an_empty_but_valid_record(self) -> None:
        merged = merge_records("CVE-1", [], {"nvd": "HTTP 503"})

        self.assertEqual(merged.cve_id, "CVE-1")
        self.assertIsNone(merged.cvss_score)
        self.assertIsNone(merged.known_exploited)
        self.assertEqual(merged.cwe_ids, [])
        self.assertEqual(merged.provenance, {})
        self.assertEqual(merged.source_errors, {"nvd": "HTTP 503"})

    def test_unknown_source_ranks_last(self) -> None:
        mystery = SourceRecord(source="osv", cve_id="CVE-1", cvss_score=5.0)

        merged = merge_records("CVE-1", [mystery, _create_mock_nvd_record()])

        self.assertEqual(merged.cvss_score, 10.0)
        self.assertEqual(merged.provenance["cvss_score"], "nvd")

    def test_audit_fields_pass_through(self) -> None:
        merged = merge_records(
            "CVE-1",
            [_create_mock_nvd_record()],
            fetched_at="2026-07-25T12:00:00+00:00",
            cache_hit=True,
        )

        self.assertEqual(merged.fetched_at, "2026-07-25T12:00:00+00:00")
        self.assertTrue(merged.cache_hit)


if __name__ == "__main__":
    unittest.main()
