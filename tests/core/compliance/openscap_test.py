"""XCCDF interpretation preserves uncertainty and selects evidence explicitly."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from kerkov.core.compliance.openscap import (
    ARF_NAMESPACE,
    RESULTS,
    XCCDF_NAMESPACES,
    parse_report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "results.xml"
NS = "{" + XCCDF_NAMESPACES[0] + "}"


def _create_mock_report(outcomes=("pass",), result_id="assessment"):
    result = etree.Element(NS + "TestResult", nsmap={None: XCCDF_NAMESPACES[0]})
    result.set("id", result_id)
    result.set("end-time", "2026-10-25T12:00:00Z")
    etree.SubElement(result, NS + "profile", idref="xccdf_example_profile_server")
    for index, outcome in enumerate(outcomes):
        rule = etree.SubElement(result, NS + "rule-result", idref=f"rule_{index}")
        etree.SubElement(rule, NS + "result").text = outcome
    return result


class OpenScapReportTest(unittest.TestCase):
    def test_preserves_every_outcome_and_multicheck_instances(self):
        parsed = parse_report(FIXTURE.read_bytes())
        self.assertEqual(parsed["rule_result_count"], 10)
        self.assertEqual(parsed["unique_rule_count"], 9)
        self.assertEqual(
            parsed["counts"], {value: 2 if value == "fail" else 1 for value in RESULTS}
        )
        self.assertTrue(parsed["has_failures"])
        self.assertTrue(parsed["has_inconclusive_results"])
        self.assertEqual(parsed["rules"][0]["result"], "error")
        self.assertEqual(parsed["benchmark_version"], "1.0")
        self.assertEqual(parsed["reported_targets"], ["server-one"])
        ssh = next(item for item in parsed["rules"] if item["rule_id"].endswith("_ssh"))
        self.assertEqual(ssh["title"], "Disable direct root SSH login")
        self.assertEqual(ssh["identifiers"], ["EXAMPLE-1"])
        permissions = [
            item for item in parsed["rules"] if item["rule_id"].endswith("_permissions")
        ]
        self.assertEqual(
            {item["instances"][0] for item in permissions},
            {"/etc/passwd", "/etc/shadow"},
        )
        self.assertNotIn("raw-secret", json.dumps(parsed))
        self.assertNotIn("score", parsed)

    def test_standalone_results_and_xccdf_11_are_supported(self):
        raw = etree.tostring(_create_mock_report()).replace(b"xccdf/1.2", b"xccdf/1.1")
        parsed = parse_report(raw)
        self.assertEqual(parsed["counts"]["pass"], 1)
        self.assertFalse(parsed["has_failures"])
        self.assertFalse(parsed["has_inconclusive_results"])
        self.assertIsNone(parsed["benchmark_id"])

    def test_arf_ignores_results_in_report_requests_and_selects_explicit_result(self):
        arf = etree.Element("{" + ARF_NAMESPACE + "}asset-report-collection")
        request = etree.SubElement(arf, "{" + ARF_NAMESPACE + "}report-requests")
        request.append(_create_mock_report(result_id="not_a_report"))
        reports = etree.SubElement(arf, "{" + ARF_NAMESPACE + "}reports")
        for name, outcome in (("before", "fail"), ("after", "pass")):
            report = etree.SubElement(reports, "{" + ARF_NAMESPACE + "}report")
            content = etree.SubElement(report, "{" + ARF_NAMESPACE + "}content")
            content.append(_create_mock_report((outcome,), result_id=name))
        raw = etree.tostring(arf)
        with self.assertRaisesRegex(ValueError, "multiple TestResults"):
            parse_report(raw)
        parsed = parse_report(raw, result_id="after")
        self.assertEqual(parsed["format"], "arf")
        self.assertEqual(parsed["counts"]["pass"], 1)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            parse_report(raw, result_id="not_a_report")

    def test_profile_must_match_the_exact_selected_result(self):
        raw = etree.tostring(_create_mock_report())
        parse_report(raw, expected_profile="xccdf_example_profile_server")
        with self.assertRaisesRegex(ValueError, "expected_profile"):
            parse_report(raw, expected_profile="server")

    def test_arf_titles_come_from_one_exact_matching_embedded_benchmark(self):
        arf = etree.Element("{" + ARF_NAMESPACE + "}asset-report-collection")
        requests = etree.SubElement(arf, "{" + ARF_NAMESPACE + "}report-requests")
        request = etree.SubElement(requests, "{" + ARF_NAMESPACE + "}report-request")
        content = etree.SubElement(request, "{" + ARF_NAMESPACE + "}content")
        benchmark = etree.fromstring(FIXTURE.read_bytes())
        result = benchmark.find(NS + "TestResult")
        benchmark.remove(result)
        content.append(benchmark)
        reports = etree.SubElement(arf, "{" + ARF_NAMESPACE + "}reports")
        report = etree.SubElement(reports, "{" + ARF_NAMESPACE + "}report")
        etree.SubElement(report, "{" + ARF_NAMESPACE + "}content").append(result)
        parsed = parse_report(etree.tostring(arf))
        ssh = next(item for item in parsed["rules"] if item["rule_id"].endswith("_ssh"))
        self.assertEqual(ssh["title"], "Disable direct root SSH login")
        self.assertEqual(parsed["benchmark_version"], "1.0")
        content.append(etree.fromstring(etree.tostring(benchmark)))
        ambiguous = parse_report(etree.tostring(arf))
        self.assertEqual(ambiguous["benchmark_version"], "")
        self.assertTrue(all(item["title"] == "" for item in ambiguous["rules"]))

    def test_no_pass_is_inferred_from_unknown_na_or_fixed(self):
        for outcome in (
            "unknown",
            "error",
            "notchecked",
            "fixed",
            "notapplicable",
            "notselected",
            "informational",
        ):
            with self.subTest(outcome=outcome):
                parsed = parse_report(etree.tostring(_create_mock_report((outcome,))))
                self.assertEqual(parsed["counts"]["pass"], 0)
                self.assertEqual(parsed["evaluated_rule_result_count"], 0)
                self.assertEqual(
                    parsed["has_inconclusive_results"],
                    outcome in {"unknown", "error", "notchecked", "fixed"},
                )

    def test_incomplete_empty_and_malformed_results_are_rejected(self):
        for mutation in (
            "end-time",
            "id",
            "outcome",
            "empty",
            "duplicate_outcome",
            "bad_date",
        ):
            result = _create_mock_report()
            if mutation in {"end-time", "id"}:
                del result.attrib[mutation]
            elif mutation == "empty":
                result.remove(result.find(NS + "rule-result"))
            elif mutation == "duplicate_outcome":
                etree.SubElement(
                    result.find(NS + "rule-result"), NS + "result"
                ).text = "pass"
            elif mutation == "bad_date":
                result.set("end-time", "unfinished")
            else:
                result.find(NS + "rule-result/" + NS + "result").text = "compliant"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                parse_report(etree.tostring(result))
        for raw in (b"<TestResult/>", b"<broken", b"<not-a-report/>"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_report(raw)

    def test_duplicate_result_ids_are_rejected_even_with_explicit_selection(self):
        benchmark = etree.Element(NS + "Benchmark")
        benchmark.extend([_create_mock_report(), _create_mock_report()])
        with self.assertRaisesRegex(ValueError, "duplicate TestResult"):
            parse_report(etree.tostring(benchmark), result_id="assessment")

    def test_xml_never_resolves_dtds_or_entities_including_utf16(self):
        raw = """<?xml version="1.0"?><!DOCTYPE TestResult [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
        <TestResult xmlns="http://checklists.nist.gov/xccdf/1.2" id="a" end-time="2026-10-25T12:00:00">
          <target>&secret;</target><rule-result idref="r"><result>pass</result></rule-result>
        </TestResult>"""
        for encoded in (
            raw.encode(),
            raw.replace('version="1.0"', 'version="1.0" encoding="UTF-16"').encode(
                "utf-16"
            ),
        ):
            with (
                self.subTest(encoding=encoded[:10]),
                self.assertRaisesRegex(ValueError, "DTD"),
            ):
                parse_report(encoded)

    def test_size_and_rule_count_limits_are_enforced(self):
        with patch("kerkov.core.compliance.openscap.MAX_REPORT_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "processing limit"):
                parse_report(b"012345")
        with patch("kerkov.core.compliance.openscap.MAX_RULE_RESULTS", 1):
            with self.assertRaisesRegex(ValueError, "rule results"):
                parse_report(etree.tostring(_create_mock_report(("pass", "fail"))))
