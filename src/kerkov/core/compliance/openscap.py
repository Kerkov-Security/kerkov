"""Bounded, offline XCCDF and ARF result interpretation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from lxml import etree  # type: ignore[import-untyped]

from kerkov.core.automation.profiles import local_file
from kerkov.core.compliance.profile_sources import (
    COMMIT_PATTERN,
    ProfileSource,
    cache_generation_lock,
)
from kerkov.core.compliance.reports import MAX_REPORT_BYTES, new_directory

MAX_RULE_RESULTS = 100_000
XCCDF_NAMESPACES = (
    "http://checklists.nist.gov/xccdf/1.2",
    "http://checklists.nist.gov/xccdf/1.1",
)
ARF_NAMESPACE = "http://scap.nist.gov/schema/asset-reporting-format/1.1"
DATASTREAM_TAG = "{http://scap.nist.gov/schema/scap/source/1.2}data-stream-collection"
RESULTS = (
    "pass",
    "fail",
    "error",
    "unknown",
    "notapplicable",
    "notchecked",
    "notselected",
    "informational",
    "fixed",
)
_RESULT_PRIORITY = {
    value: index
    for index, value in enumerate(
        (
            "error",
            "fail",
            "unknown",
            "notchecked",
            "fixed",
            "informational",
            "notapplicable",
            "notselected",
            "pass",
        )
    )
}
CONTROL_STATUSES = {
    "pass": "passed",
    "fail": "failed",
    "error": "error",
    "unknown": "error",
    "notapplicable": "skipped",
    "notchecked": "no_tests",
    "notselected": "skipped",
    "informational": "no_tests",
    "fixed": "no_tests",
}


def _text(element: Any, limit: int = 500) -> str:
    return (
        " ".join("".join(element.itertext()).split())[:limit]
        if element is not None
        else ""
    )


def _required(value: str | None, label: str) -> str:
    if not value or not value.strip() or len(value) > 1024:
        raise ValueError(f"Missing or invalid XCCDF {label}")
    return value


def _result_elements(root: Any) -> list[Any]:
    if root.tag == f"{{{ARF_NAMESPACE}}}asset-report-collection":
        contents = root.findall(
            f"{{{ARF_NAMESPACE}}}reports/{{{ARF_NAMESPACE}}}report/{{{ARF_NAMESPACE}}}content"
        )
    elif any(
        root.tag == f"{{{ns}}}{name}"
        for ns in XCCDF_NAMESPACES
        for name in ("Benchmark", "TestResult")
    ):
        contents = [root]
    else:
        raise ValueError(
            "Expected an XCCDF Benchmark/TestResult or ARF report collection"
        )
    results = [
        element
        for content in contents
        for element in content.iter()
        if element.tag in {f"{{{ns}}}TestResult" for ns in XCCDF_NAMESPACES}
    ]
    if not results or len(results) > 100:
        raise ValueError("Report must contain between 1 and 100 XCCDF TestResults")
    return results


def _benchmark_for_result(root: Any, result: Any, prefix: str) -> Any:
    for parent in result.iterancestors():
        if parent.tag == prefix + "Benchmark":
            return parent
    reference = result.find(prefix + "benchmark")
    if reference is None or not reference.get("id"):
        return None
    contents = root.findall(
        f"{{{ARF_NAMESPACE}}}report-requests/{{{ARF_NAMESPACE}}}report-request/{{{ARF_NAMESPACE}}}content"
    )
    candidates = [
        benchmark
        for content in contents
        for benchmark in content.iter(prefix + "Benchmark")
        if benchmark.get("id") == reference.get("id")
    ]
    return candidates[0] if len(candidates) == 1 else None


def _parse_xml(raw: bytes, label: str) -> Any:
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"OpenSCAP {label} exceeds the 64 MiB processing limit")
    try:
        parser = etree.XMLParser(
            resolve_entities=False, load_dtd=False, no_network=True, recover=False
        )
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"Invalid OpenSCAP XML {label}") from exc
    if root.getroottree().docinfo.doctype:
        raise ValueError(f"OpenSCAP {label} must not contain a DTD")
    return root


def inspect_datastream(raw: bytes) -> dict[str, Any]:
    """Discover profiles in prepared content without executing or fetching resources."""
    root = _parse_xml(raw, "datastream")
    if root.tag != DATASTREAM_TAG:
        raise ValueError(
            "Choose a prepared SCAP source datastream XML, not source requiring a build"
        )
    source_ns = DATASTREAM_TAG.removesuffix("data-stream-collection")
    identifiers_list = [
        element.get("id")
        for element in root.iter()
        if isinstance(element.tag, str)
        and element.tag.startswith(source_ns)
        and element.get("id")
    ]
    identifiers = set(identifiers_list)
    if len(identifiers) != len(identifiers_list):
        raise ValueError("Datastream contains ambiguous duplicate IDs")
    for reference in root.iter(source_ns + "component-ref"):
        href = reference.get("{http://www.w3.org/1999/xlink}href", "")
        if not href.startswith("#") or href[1:] not in identifiers:
            raise ValueError(
                "Datastream component references must resolve inside this XML"
            )
    for reference in root.iter("{urn:oasis:names:tc:entity:xmlns:xml:catalog}uri"):
        uri = reference.get("uri", "")
        if not uri.startswith("#") or uri[1:] not in identifiers:
            raise ValueError(
                "Datastream catalog references must resolve inside this XML"
            )
    profiles = []
    profile_ids: set[str] = set()
    benchmarks = []
    for ns in XCCDF_NAMESPACES:
        prefix = f"{{{ns}}}"
        for benchmark in root.iter(prefix + "Benchmark"):
            benchmark_id = _required(benchmark.get("id"), "benchmark id")
            title = _text(benchmark.find(prefix + "title")) or benchmark_id
            version = _text(benchmark.find(prefix + "version"), 100)
            benchmarks.append({"id": benchmark_id, "title": title, "version": version})
            for profile in benchmark.findall(prefix + "Profile"):
                profile_id = _required(profile.get("id"), "profile id")
                if profile_id in profile_ids:
                    raise ValueError(
                        "Datastream contains ambiguous duplicate profile IDs"
                    )
                profile_ids.add(profile_id)
                if not re.fullmatch(r"xccdf_[A-Za-z0-9_.:-]+", profile_id):
                    raise ValueError("Datastream has an unsupported XCCDF profile ID")
                profiles.append(
                    {
                        "id": profile_id,
                        "title": _text(profile.find(prefix + "title")) or profile_id,
                        "description": _text(profile.find(prefix + "description")),
                        "benchmark_id": benchmark_id,
                    }
                )
                if len(profiles) > 1000:
                    raise ValueError("Datastream exceeds the 1000 profile limit")
    if not profiles:
        raise ValueError("Datastream contains no selectable XCCDF profiles")
    return {
        "engine": "openscap",
        "name": benchmarks[0]["title"],
        "version": benchmarks[0]["version"],
        "profile_sha256": hashlib.sha256(raw).hexdigest(),
        "benchmarks": benchmarks,
        "profiles": profiles,
    }


def datastream_bytes(
    path: Path,
    expected_sha256: str,
    profile_id: str,
    *,
    projects_root: Path | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> bytes:
    """Read the exact reviewed SCAP source datastream before staging it."""
    item = None
    if projects_root is not None:
        root = projects_root.expanduser().resolve()
        if path.is_relative_to(root):
            parts = path.relative_to(root).parts
            if (
                len(parts) == 5
                and parts[1] == "versions"
                and parts[3:] == ("profile", "datastream.xml")
                and re.fullmatch(r"[a-f0-9]{64}", parts[0])
                and re.fullmatch(r"[a-f0-9]{32}", parts[2])
            ):
                item = root / parts[0]
    with cache_generation_lock(item) if item is not None else nullcontext():
        with path.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
        if item is not None and source_metadata is not None:
            origin = path.parent / ".kerkov-source.json"
            if origin.is_symlink():
                raise ValueError(
                    "Cached datastream source record must be an ordinary file"
                )
            with origin.open("rb") as handle:
                provenance = json.loads(handle.read(8193))
            if (
                not isinstance(provenance, dict)
                or ProfileSource.parse(
                    provenance.get("url"), datastream=provenance.get("datastream")
                ).key
                != item.name
                or not COMMIT_PATTERN.fullmatch(str(provenance.get("commit", "")))
                or provenance.get("profile_sha256") != expected_sha256
            ):
                raise ValueError("Cached datastream source record is invalid")
            source_metadata.update(provenance)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("OpenSCAP datastream exceeds the 64 MiB processing limit")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("OpenSCAP datastream does not match the reviewed SHA-256")
    root = _parse_xml(raw, "datastream")
    if root.tag != DATASTREAM_TAG:
        raise ValueError("Choose a self-contained SCAP source datastream")
    profiles = (
        item for ns in XCCDF_NAMESPACES for item in root.iter(f"{{{ns}}}Profile")
    )
    if not any(item.get("id") == profile_id for item in profiles):
        raise ValueError(
            "The SCAP datastream does not contain the selected XCCDF profile"
        )
    return raw


def parse_report(
    raw: bytes, *, result_id: str | None = None, expected_profile: str | None = None
) -> dict[str, Any]:
    root = _parse_xml(raw, "report")
    candidates = _result_elements(root)
    available = [_required(item.get("id"), "TestResult id") for item in candidates]
    if len(set(available)) != len(available):
        raise ValueError("Report contains duplicate TestResult ids")
    if result_id is None and len(candidates) != 1:
        raise ValueError(
            "Report contains multiple TestResults; select an explicit result_id"
        )
    if result_id is not None and result_id not in available:
        raise ValueError("Requested result_id does not exist in the report")
    result = (
        candidates[available.index(result_id)]
        if result_id is not None
        else candidates[0]
    )
    ns = etree.QName(result).namespace
    prefix = f"{{{ns}}}"
    ended = _required(result.get("end-time"), "end-time")
    try:
        if "T" not in ended:
            raise ValueError("expected a date and time")
        datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "Invalid XCCDF end-time; completed assessment evidence is required"
        ) from exc
    profile_element = result.find(prefix + "profile")
    profile = (
        _required(profile_element.get("idref"), "profile idref")
        if profile_element is not None
        else None
    )
    if expected_profile is not None and profile != expected_profile:
        raise ValueError(
            "Report profile does not match expected_profile (use the full profile id)"
        )
    benchmark = _benchmark_for_result(root, result, prefix)
    definitions: dict[str, Any] = {}
    if benchmark is not None:
        for rule in benchmark.iter(prefix + "Rule"):
            rule_id = _required(rule.get("id"), "Rule id")
            if rule_id in definitions:
                raise ValueError("Benchmark contains duplicate Rule ids")
            definitions[rule_id] = rule
    rule_results = result.findall(prefix + "rule-result")
    if not rule_results or len(rule_results) > MAX_RULE_RESULTS:
        raise ValueError("TestResult must contain between 1 and 100000 rule results")
    rules: list[dict[str, Any]] = []
    for item in rule_results:
        rule_id = _required(item.get("idref"), "rule-result idref")
        outcomes = item.findall(prefix + "result")
        outcome = _text(outcomes[0]) if len(outcomes) == 1 else ""
        if outcome not in RESULTS:
            raise ValueError(
                "Rule result has a missing, duplicate or unsupported outcome"
            )
        definition = definitions.get(rule_id)
        severity = item.get("severity") or (
            definition.get("severity") if definition is not None else None
        )
        rules.append(
            {
                "rule_id": rule_id,
                "result": outcome,
                "severity": (
                    severity
                    if severity in {"unknown", "info", "low", "medium", "high"}
                    else "unknown"
                ),
                "title": (
                    _text(definition.find(prefix + "title"))
                    if definition is not None
                    else ""
                ),
                "identifiers": [
                    _text(ident, 256)
                    for ident in (
                        item.findall(prefix + "ident")
                        or (
                            definition.findall(prefix + "ident")
                            if definition is not None
                            else []
                        )
                    )[:20]
                ],
                "instances": [
                    _text(instance, 256)
                    for instance in item.findall(prefix + "instance")[:20]
                ],
            }
        )
    counts = Counter(item["result"] for item in rules)
    rules.sort(key=lambda item: (_RESULT_PRIORITY[item["result"]], item["rule_id"]))
    benchmark_ref = result.find(prefix + "benchmark")
    benchmark_id = (
        benchmark.get("id")
        if benchmark is not None
        else (benchmark_ref.get("id") if benchmark_ref is not None else None)
    )
    if benchmark_id is not None:
        benchmark_id = _required(benchmark_id, "benchmark id")
    return {
        "format": (
            "arf"
            if root.tag == f"{{{ARF_NAMESPACE}}}asset-report-collection"
            else "xccdf"
        ),
        "result_id": result.get("id"),
        "profile_id": profile,
        "benchmark_id": benchmark_id,
        "benchmark_version": (
            _text(benchmark.find(prefix + "version")) if benchmark is not None else ""
        ),
        "test_system": (result.get("test-system") or "")[:1024],
        "started_at": (result.get("start-time") or "")[:100],
        "ended_at": ended,
        "reported_targets": [
            _text(item, 256) for item in result.findall(prefix + "target")[:20]
        ],
        "rule_result_count": len(rules),
        "unique_rule_count": len({item["rule_id"] for item in rules}),
        "evaluated_rule_result_count": counts["pass"] + counts["fail"],
        "counts": {value: counts[value] for value in RESULTS},
        "has_failures": bool(counts["fail"]),
        "has_inconclusive_results": any(
            counts[value] for value in ("error", "unknown", "notchecked", "fixed")
        ),
        "rules": rules,
    }


def assessment_controls(
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[tuple[str, str], list[dict[str, Any]]]]:
    """Project XCCDF rules into the shared control view while retaining instances."""
    profile = report["profile_id"]
    instances: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rule in report["rules"]:
        instances.setdefault((profile, rule["rule_id"]), []).append(rule)
    controls = [
        {
            "profile": profile,
            "control_id": rule_id,
            "title": rules[0]["title"],
            "impact": None,
            "status": CONTROL_STATUSES[rules[0]["result"]],
            "xccdf_result": rules[0]["result"],
        }
        for (_, rule_id), rules in instances.items()
    ]
    controls.sort(key=lambda control: control["xccdf_result"] == "notselected")
    counts = dict.fromkeys(
        ("passed", "failed", "error", "skipped", "waived", "no_tests"), 0
    )
    for control in controls:
        counts[control["status"]] += 1
    return {
        "profiles": [{"name": profile, "version": report["benchmark_version"]}],
        "profile_count": 1,
        "profile_error_count": 0,
        "control_count": len(controls),
        "counts": counts,
        "xccdf_counts": report["counts"],
        "has_inconclusive_results": report["has_inconclusive_results"],
        "controls": controls,
    }, instances


def import_report(
    workspace: Path,
    report: str,
    *,
    result_id: str | None,
    expected_profile: str | None,
    max_rules: int | None,
    evidence_status: Literal["imported", "scanned"] = "imported",
) -> dict[str, Any]:
    source = local_file(report, workspace)
    with source.open("rb") as handle:
        raw = handle.read(MAX_REPORT_BYTES + 1)
    parsed = parse_report(raw, result_id=result_id, expected_profile=expected_profile)
    directory = new_directory(workspace, "report-")
    evidence = directory / "source.xml"
    normalized = directory / "results.json"
    evidence.write_bytes(raw)
    provenance = {
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(source),
        "report_artifact": str(evidence),
        "normalized_artifact": str(normalized),
        "evidence_status": evidence_status,
    }
    normalized.write_text(
        json.dumps({**parsed, **provenance}, indent=2), encoding="utf-8"
    )
    return {
        **parsed,
        **provenance,
        "rules": parsed["rules"][:max_rules],
        "rules_truncated": max_rules is not None and len(parsed["rules"]) > max_rules,
        "notice": "Imported scanner observations, not an independent attestation. Correlate reported targets with the reviewed inventory; check the Ansible recap for missing hosts. Fixed results require a fresh assessment. No ledger entries were changed.",
    }
