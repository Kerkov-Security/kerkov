"""Interpret Cinc Auditor/InSpec JSON without exposing test payloads."""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_CONTROLS = 100_000
_STATUSES = ("passed", "failed", "error", "skipped", "no_tests", "waived")
_PRIORITY = {
    value: index
    for index, value in enumerate(
        ("error", "failed", "skipped", "no_tests", "waived", "passed")
    )
}


def _text(value: Any, limit: int = 500) -> str:
    return value[:limit] if isinstance(value, str) else ""


def parse_report(raw: bytes, *, expected_profile: str | None = None) -> dict[str, Any]:
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Cinc report exceeds the 64 MiB processing limit")
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid Cinc/InSpec JSON report") from exc
    if not isinstance(report, dict) or not isinstance(report.get("platform"), dict):
        raise ValueError("Expected a Cinc/InSpec JSON report with platform metadata")
    profiles = report.get("profiles")
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 100:
        raise ValueError("Cinc report must contain between 1 and 100 profiles")
    if not isinstance(report.get("version"), str) or not report["version"].strip():
        raise ValueError("Cinc report is missing its scanner version")
    controls: list[dict[str, Any]] = []
    profile_summaries = []
    test_totals: Counter[str] = Counter()
    for profile in profiles:
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            raise ValueError("Invalid Cinc profile metadata")
        name = profile["name"]
        if not name.strip() or len(name) > 1024:
            raise ValueError("Invalid Cinc profile name")
        entries = profile.get("controls", [])
        if not isinstance(entries, list) or len(controls) + len(entries) > MAX_CONTROLS:
            raise ValueError("Invalid or oversized Cinc control list")
        status = profile.get("status", "loaded")
        if not isinstance(status, str):
            raise ValueError("Invalid Cinc profile status")
        if status not in {"loaded", "skipped", "failed"}:
            status = "error"
        profile_summaries.append(
            {
                "name": name,
                "version": _text(profile.get("version"), 100),
                "sha256": _text(profile.get("sha256"), 128),
                "status": status,
                "control_count": len(entries),
            }
        )
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not entry["id"].strip()
                or len(entry["id"]) > 1024
            ):
                raise ValueError("Invalid Cinc control id")
            results = entry.get("results", [])
            if not isinstance(results, list) or len(results) > 100_000:
                raise ValueError("Invalid or oversized Cinc test results")
            counts: Counter[str] = Counter()
            for result in results:
                if not isinstance(result, dict) or not isinstance(
                    result.get("status"), str
                ):
                    raise ValueError("Missing or unsupported Cinc test status")
                outcome = result["status"]
                if result.get("exception") or outcome not in {
                    "passed",
                    "failed",
                    "skipped",
                    "error",
                }:
                    outcome = "error"
                counts[outcome] += 1
                test_totals[outcome] += 1
            outcome = next(
                (value for value in ("error", "failed", "skipped") if counts[value]),
                "passed" if results else "no_tests",
            )
            waiver = entry.get("waiver_data")
            if (
                isinstance(waiver, dict)
                and waiver.get("skipped_due_to_waiver") is True
                and not counts["error"]
                and not counts["failed"]
            ):
                outcome = "waived"
            impact = entry.get("impact")
            if (
                not isinstance(impact, (int, float))
                or isinstance(impact, bool)
                or not 0 <= impact <= 1
                or not math.isfinite(impact)
            ):
                impact = None
            controls.append(
                {
                    "profile": name,
                    "control_id": entry["id"],
                    "title": _text(entry.get("title")),
                    "impact": impact,
                    "status": outcome,
                    "test_counts": {
                        value: counts[value]
                        for value in ("passed", "failed", "skipped", "error")
                    },
                }
            )
    if expected_profile is not None and not any(
        profile["name"] == expected_profile for profile in profile_summaries
    ):
        raise ValueError("Cinc report does not contain expected_profile")
    counts = Counter(control["status"] for control in controls)
    controls.sort(
        key=lambda item: (
            _PRIORITY[item["status"]],
            -(item["impact"] or 0),
            item["profile"],
            item["control_id"],
        )
    )
    return {
        "format": "cinc",
        "scanner_version": _text(report["version"], 100),
        "platform": {
            key: _text(report["platform"].get(key))
            for key in ("name", "release", "target_id")
        },
        "profiles": profile_summaries,
        "control_count": len(controls),
        "counts": {value: counts[value] for value in _STATUSES},
        "test_counts": {
            value: test_totals[value]
            for value in ("passed", "failed", "skipped", "error")
        },
        "has_failures": bool(test_totals["failed"]),
        "has_inconclusive_results": not controls
        or any(counts[value] for value in ("error", "skipped", "no_tests", "waived"))
        or any(profile["status"] != "loaded" for profile in profile_summaries),
        "profile_error_count": sum(
            profile["status"] in {"failed", "error"} for profile in profile_summaries
        ),
        "controls": controls,
    }
