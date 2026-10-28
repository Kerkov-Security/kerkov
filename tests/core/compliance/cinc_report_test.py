"""Cinc evidence preserves failed, skipped, errored and waived coverage."""

import json
import unittest
from pathlib import Path

from kerkov.core.compliance.cinc_report import parse_report

FIXTURE = Path(__file__).parent / "fixtures/cinc.json"


class CincReportTest(unittest.TestCase):
    def test_normalizes_control_outcomes_without_raw_test_payloads(self):
        report = parse_report(FIXTURE.read_bytes(), expected_profile="fixture")
        self.assertEqual(
            report["counts"],
            {
                "passed": 1,
                "failed": 1,
                "skipped": 1,
                "error": 1,
                "no_tests": 1,
                "waived": 1,
            },
        )
        self.assertEqual(report["controls"][0]["status"], "error")
        self.assertTrue(report["has_failures"])
        self.assertTrue(report["has_inconclusive_results"])
        self.assertEqual(report["test_counts"]["failed"], 1)
        self.assertNotIn("raw-secret", json.dumps(report))

    def test_waivers_only_suppress_explicitly_skipped_controls(self):
        for waiver in (
            {"run": True, "skipped_due_to_waiver": False},
            {
                "run": False,
                "skipped_due_to_waiver": False,
                "message": "Waiver expired, evaluating control normally",
            },
            {"run": False},
            {"skipped_due_to_waiver": "true"},
            "unexpected",
            {"skipped_due_to_waiver": True},
        ):
            for outcome in ("failed", "error"):
                with self.subTest(waiver=waiver, outcome=outcome):
                    report = json.loads(FIXTURE.read_bytes())
                    control = {
                        "id": "evaluated",
                        "waiver_data": waiver,
                        "results": [{"status": outcome}],
                    }
                    report["profiles"][0]["controls"] = [control]
                    parsed = parse_report(json.dumps(report).encode())
                    self.assertEqual(parsed["counts"][outcome], 1)
                    self.assertEqual(parsed["counts"]["waived"], 0)
                    self.assertEqual(parsed["controls"][0]["status"], outcome)

    def test_nonsuppressing_waiver_keeps_pass_and_empty_outcomes(self):
        for results, expected in (([{"status": "passed"}], "passed"), ([], "no_tests")):
            with self.subTest(expected=expected):
                report = json.loads(FIXTURE.read_bytes())
                report["profiles"][0]["controls"] = [
                    {
                        "id": "evaluated",
                        "waiver_data": {"run": True},
                        "results": results,
                    }
                ]
                parsed = parse_report(json.dumps(report).encode())
                self.assertEqual(parsed["controls"][0]["status"], expected)

    def test_unsupported_empty_or_failed_profiles_never_imply_pass(self):
        for status in ("skipped", "failed", "loaded"):
            report = json.loads(FIXTURE.read_bytes())
            report["profiles"][0].update(status=status, controls=[])
            parsed = parse_report(json.dumps(report).encode())
            self.assertEqual(parsed["counts"]["passed"], 0)
            self.assertTrue(parsed["has_inconclusive_results"])
            self.assertEqual(parsed["profile_error_count"], int(status == "failed"))

    def test_invalid_or_incomplete_json_is_rejected_without_echoing_values(self):
        for raw in (
            b'{"raw-secret":',
            b"{}",
            b"[]",
            b'{"platform":{},"version":"7","profiles":[]}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as caught:
                parse_report(raw)
            self.assertNotIn("raw-secret", str(caught.exception))
        report = json.loads(FIXTURE.read_bytes())
        for value in (None, []):
            report["profiles"][0]["controls"][0]["results"][0]["status"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_report(json.dumps(report).encode())

    def test_unknown_statuses_are_errors_and_other_controls_survive(self):
        report = json.loads(FIXTURE.read_bytes())
        report["profiles"][0]["status"] = "future-profile-state"
        report["profiles"][0]["controls"][0]["results"][0][
            "status"
        ] = "future-test-state"
        parsed = parse_report(json.dumps(report).encode())
        self.assertEqual(parsed["profiles"][0]["status"], "error")
        self.assertEqual(parsed["profile_error_count"], 1)
        self.assertEqual(parsed["counts"]["error"], 2)
        self.assertEqual(parsed["counts"]["passed"], 0)
        self.assertEqual(parsed["counts"]["failed"], 1)
        self.assertEqual(parsed["control_count"], 6)
        self.assertTrue(parsed["has_inconclusive_results"])

    def test_expected_profile_and_impact_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "expected_profile"):
            parse_report(FIXTURE.read_bytes(), expected_profile="different")
        report = json.loads(FIXTURE.read_bytes())
        report["profiles"][0]["controls"][0]["impact"] = 10**400
        parsed = parse_report(json.dumps(report).encode())
        self.assertIsNone(
            next(item for item in parsed["controls"] if item["control_id"] == "pass")[
                "impact"
            ]
        )
