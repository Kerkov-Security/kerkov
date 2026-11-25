"""Local evidence reviews and complete, portable assessment exports."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.compliance.cinc_report import decode_report, parse_report
from kerkov.core.compliance.review import (
    ControlQuery,
    ReviewEvidence,
    browse_controls,
    build_report,
    export_report,
    review_control,
)
from kerkov.core.compliance.workflows import WorkflowRequest, new_record


def _create_mock_record(workspace, source=None):
    raw = source or json.loads(
        (Path(__file__).parent / "fixtures/cinc.json").read_bytes()
    )
    directory = workspace / "artifacts/compliance/scan-fixture"
    directory.mkdir(parents=True, exist_ok=True)
    evidence = directory / "source.json"
    evidence.write_text(json.dumps(raw))
    parsed = parse_report(evidence.read_bytes())
    request = WorkflowRequest.model_validate(
        {
            "action": "assess",
            "resource_id": 1,
            "scan": {
                "host": "192.0.2.1",
                "user": "audit",
                "profile": "/profiles/fixture",
                "private_key_file": "/private/key",
                "known_hosts_file": "/private/trust",
                "inputs_file": "/private/inputs.yml",
            },
        }
    )
    record = new_record(request, "Fixture baseline")
    record.update(status="attention", stage="finished", detail="Unresolved controls")
    record["plan"] = {
        "baseline": {
            "profile_name": "fixture",
            "name": "Fixture baseline",
            "version": "1.0",
            "remediation": {"fail": "Apply the reviewed file permission fix."},
        },
    }
    record["before"] = {
        **parsed,
        "controls": parsed["controls"][:2],
        "controls_truncated": True,
        "report_artifact": str(evidence),
        "report_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
        "normalized_artifact": str(directory / "normalized.json"),
        "manifest_artifact": str(directory / "manifest.json"),
        "status": "completed",
        "evidence_status": "scanned",
        "profile_name": "fixture",
        "profile_sha256": "a" * 64,
        "image": "fixture/cinc:7",
        "image_id": "sha256:" + "b" * 64,
        "target": {"host": "192.0.2.1", "port": 22, "user": "audit"},
    }
    return record


def test_report_uses_complete_evidence_preserves_outcomes_and_provenance(tmp_path):
    record = _create_mock_record(tmp_path)
    report = build_report(tmp_path, record)
    assert len(report["controls"]) == 6
    assert {control["before"] for control in report["controls"]} == {
        "passed",
        "failed",
        "skipped",
        "error",
        "no_tests",
        "waived",
    }
    assert {control["after"] for control in report["controls"]} == {"unassessed"}
    assert report["assessments"]["after"] is None
    before = report["assessments"]["before"]
    for key in ("report_sha256", "profile_sha256", "image_id", "target"):
        assert before[key] == record["before"][key]
    assert before["scanner_version"] == "7.3.4"
    assert report["baseline"]["version"] == "1.0"
    assert report["asset"]["resource_id"] == 1
    assert len(record["before"]["controls"]) == 2


def test_external_project_identity_is_preserved_in_json_exports(tmp_path):
    record = _create_mock_record(tmp_path)
    bundle = {
        "name": "external",
        "version": "2.0",
        "dependencies": [{"name": "shared", "cache_key": "a" * 64}],
    }
    record["before"]["profile_bundle"] = bundle
    record["plan"]["profile_bundle"] = bundle
    report = json.loads(export_report(tmp_path, record, "json")["body"])
    assert report["assessments"]["before"]["profile_bundle"] == bundle
    assert report["plan"]["profile_bundle"] == bundle


def test_review_keeps_assertion_messages_local_and_uses_saved_mapping(tmp_path):
    record = _create_mock_record(tmp_path)
    detail = review_control(tmp_path, record, "fixture", "fail")
    assert detail["before"]["status"] == "failed"
    assert detail["before"]["tests"][0]["message"] == "raw-secret-failure"
    assert detail["after"]["status"] == "unassessed"
    assert detail["remediation"] == "Apply the reviewed file permission fix."
    assert (
        "Manual review required"
        in review_control(tmp_path, record, "fixture", "skip")["remediation"]
    )
    assert "raw-secret" not in json.dumps(record)


def test_exports_include_assertions_but_exclude_inputs_source_and_ssh_files(tmp_path):
    record = _create_mock_record(tmp_path)
    exported = export_report(tmp_path, record, "json")
    assert exported["mime"] == "application/json"
    assert "raw-secret-failure" in exported["body"]
    for excluded in (
        "raw-secret-input",
        "raw-secret-source",
        "/private/key",
        "/private/inputs.yml",
    ):
        assert excluded not in exported["body"]
    assert "not overall compliance certification" in exported["body"]


def test_html_is_standalone_and_escapes_untrusted_evidence(tmp_path):
    source = json.loads((Path(__file__).parent / "fixtures/cinc.json").read_bytes())
    source["profiles"][0]["controls"][1]["results"][0].update(
        code_desc='<script src="https://example.invalid/payload"></script>',
        message="expected 0644, observed 0666",
    )
    record = _create_mock_record(tmp_path, source)
    exported = export_report(tmp_path, record, "html")
    assert exported["mime"] == "text/html"
    assert "<script" not in exported["body"]
    assert "&lt;script" in exported["body"]
    assert "expected 0644, observed 0666" in exported["body"]
    assert "Control outcomes (6)" in exported["body"]
    assert "default-src 'none'" in exported["body"]


def test_report_compares_verification_without_closing_failed_controls(tmp_path):
    record = _create_mock_record(tmp_path)
    record["request"]["action"] = "apply"
    record["status"] = "failed"
    record["remediation"] = {"success": False, "check": False, "exit_code": 2}
    record["after"] = copy.deepcopy(record["before"])
    report = build_report(tmp_path, record)
    assert report["run"]["status"] == "failed"
    failed = next(c for c in report["controls"] if c["control_id"] == "fail")
    assert failed["before"] == failed["after"] == "failed"
    assert report["remediation"]["success"] is False


def test_assertions_and_individual_strings_are_bounded_with_visible_notices(tmp_path):
    source = json.loads((Path(__file__).parent / "fixtures/cinc.json").read_bytes())
    source["profiles"][0]["controls"][1]["results"] = [
        {"status": "failed", "message": "x" * 3000} for _ in range(25)
    ]
    record = _create_mock_record(tmp_path, source)
    detail = review_control(tmp_path, record, "fixture", "fail")["before"]
    assert detail["test_count"] == 25
    assert len(detail["tests"]) == 20
    assert detail["tests_truncated"] is True
    assert detail["tests"][0]["message"].endswith("[Text truncated]")


@pytest.mark.parametrize("failure", ["changed", "outside", "symlink", "missing"])
def test_unavailable_or_changed_evidence_never_exports_a_clean_result(
    tmp_path, failure
):
    workspace = tmp_path / "workspace"
    record = _create_mock_record(workspace)
    path = Path(record["before"]["report_artifact"])
    if failure == "changed":
        path.write_text("{}")
    elif failure in {"outside", "symlink"}:
        outside = tmp_path / "outside.json"
        outside.write_bytes(path.read_bytes())
        if failure == "outside":
            record["before"]["report_artifact"] = str(outside)
        else:
            path.unlink()
            path.symlink_to(outside)
    else:
        path.unlink()
    with pytest.raises((ValueError, OSError)):
        export_report(workspace, record, "json")
    detail = review_control(workspace, record, "fixture", "fail")
    assert detail["before"]["status"] == "unavailable"
    assert detail["before"]["tests"] == []


def test_duplicate_control_ids_are_rejected_as_ambiguous(tmp_path):
    source = json.loads((Path(__file__).parent / "fixtures/cinc.json").read_bytes())
    source["profiles"][0]["controls"].append(source["profiles"][0]["controls"][0])
    record = _create_mock_record(tmp_path, source)
    with pytest.raises(ValueError, match="duplicate"):
        export_report(tmp_path, record, "json")


def test_export_validates_format_and_size_and_handles_no_assessment(
    tmp_path, monkeypatch
):
    record = _create_mock_record(tmp_path)
    with pytest.raises(ValueError, match="HTML or JSON"):
        export_report(tmp_path, record, "pdf")
    record.update(before=None, status="running")
    report = build_report(tmp_path, record)
    assert report["run"]["status"] == "incomplete"
    assert report["controls"] == []
    assert report["assessments"] == {"before": None, "after": None}
    monkeypatch.setattr("kerkov.core.compliance.review.MAX_EXPORT_BYTES", 10)
    with pytest.raises(ValueError, match="export limit"):
        export_report(tmp_path, record, "json")


def test_assertion_errors_match_control_outcome_in_review_and_exports(tmp_path):
    source = json.loads((Path(__file__).parent / "fixtures/cinc.json").read_bytes())
    source["profiles"][0]["controls"][1]["results"] = [
        {"status": "passed", "exception": "Scanner exception"},
        {"status": "unknown"},
    ]
    record = _create_mock_record(tmp_path, source)
    detail = review_control(tmp_path, record, "fixture", "fail")["before"]
    assert detail["status"] == "error"
    assert [test["status"] for test in detail["tests"]] == ["error", "error"]
    exported = json.loads(export_report(tmp_path, record, "json")["body"])
    control = next(c for c in exported["controls"] if c["control_id"] == "fail")
    assert control["before_evidence"]["tests"] == detail["tests"]
    assert "Status: error" in export_report(tmp_path, record, "html")["body"]


def test_review_reuses_parsed_evidence_but_rechecks_integrity(tmp_path):
    record = _create_mock_record(tmp_path)
    evidence = ReviewEvidence()
    with patch(
        "kerkov.core.compliance.review.decode_report", wraps=decode_report
    ) as decode:
        review_control(tmp_path, record, "fixture", "fail", evidence=evidence)
        review_control(tmp_path, record, "fixture", "skip", evidence=evidence)
        export_report(tmp_path, record, "json", evidence=evidence)
        decode.assert_called_once()
        Path(record["before"]["report_artifact"]).write_text("{}")
        detail = review_control(tmp_path, record, "fixture", "fail", evidence=evidence)
        assert detail["before"]["status"] == "unavailable"
        with pytest.raises(ValueError, match="SHA-256"):
            export_report(tmp_path, record, "json", evidence=evidence)


def test_openscap_html_and_json_exports_include_bounded_benchmark_guidance(tmp_path):
    from kerkov.core.compliance.openscap import assessment_controls
    from kerkov.core.compliance.openscap import parse_report as parse_openscap

    record = _create_mock_record(tmp_path)
    directory = Path(record["before"]["report_artifact"]).parent
    evidence = directory / "results.xml"
    raw = (Path(__file__).parent / "fixtures/results.xml").read_bytes()
    evidence.write_bytes(raw)
    parsed = parse_openscap(raw)
    summary, _ = assessment_controls(parsed)
    assert "profile_error_count" not in summary
    record["before"] = {
        **summary,
        "engine": "openscap",
        "profile_name": parsed["profile_id"],
        "report_artifact": str(evidence),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
    }
    for format in ("html", "json"):
        exported = export_report(tmp_path, record, format)
        assert "Individual accounts preserve accountability." in exported["body"]
        assert "Set PermitRootLogin to no" in exported["body"]
        assert "raw-secret" not in exported["body"]
    assert "Status: failed" in export_report(tmp_path, record, "html")["body"]
    page = browse_controls(tmp_path, record, ControlQuery(impact="unknown"))
    assert page["total"] == len(summary["controls"])
    assert all(control["impact"] is None for control in page["controls"])


def test_waiver_details_survive_review_and_both_exports(tmp_path):
    from kerkov.core.compliance.openscap import assessment_controls, parse_report

    record = _create_mock_record(tmp_path)
    source = Path(record["before"]["report_artifact"]).parent / "overrides.xml"
    raw = (Path(__file__).parent / "fixtures/openscap-overrides.xml").read_bytes()
    source.write_bytes(raw)
    parsed = parse_report(raw)
    summary, _ = assessment_controls(parsed)
    record["before"] = {
        **summary,
        "engine": "openscap",
        "profile_name": parsed["profile_id"],
        "report_artifact": str(source),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
    }
    for format_name in ("html", "json"):
        body = export_report(tmp_path, record, format_name)["body"]
        assert "waived" in body
        assert "Example operator" in body
        assert "Accepted exception" in body


def _create_mock_large_record(workspace):
    source = json.loads((Path(__file__).parent / "fixtures/cinc.json").read_bytes())
    source["profiles"][0]["controls"] = [
        {
            "id": f"control-{index:03}",
            "title": f"{'SSH configuration' if index >= 269 else 'System protection'} {index:03}",
            "impact": (None, 0.3, 0.4, 0.7, 1.0)[index % 5],
            "results": [{"status": "failed" if index % 3 == 0 else "passed"}],
        }
        for index in range(275)
    ]
    return _create_mock_record(workspace, source)


def test_control_browser_paginates_complete_evidence_without_raw_assertions(tmp_path):
    record = _create_mock_large_record(tmp_path)
    record["controls"] = record["before"]["controls"][:2]
    result = browse_controls(
        tmp_path, record, ControlQuery(page=11, page_size=25, sort="control_id")
    )
    assert result["total"] == result["matched"] == 275
    assert result["pages"] == 11
    assert result["controls"][0]["control_id"] == "control-250"
    assert result["controls"][-1]["control_id"] == "control-274"
    assert "results" not in json.dumps(result)
    assert len(record["before"]["controls"]) == 2
    result = browse_controls(
        tmp_path, record, ControlQuery(search="SSH", outcome="failed")
    )
    assert result["matched"] == 2
    assert {item["control_id"] for item in result["controls"]} == {
        "control-270",
        "control-273",
    }
    assert browse_controls(tmp_path, record, ControlQuery(page=999))["page"] == 6


@pytest.mark.parametrize(
    "band,expected",
    [("unknown", {None}), ("low", {0.3}), ("medium", {0.4}), ("high", {0.7, 1.0})],
)
def test_control_browser_impact_bands_and_sorting(tmp_path, band, expected):
    record = _create_mock_large_record(tmp_path)
    result = browse_controls(
        tmp_path, record, ControlQuery(impact=band, page_size=100, sort="impact_desc")
    )
    assert {item["impact"] for item in result["controls"]} == expected
    impacts = [
        item["impact"] for item in result["controls"] if item["impact"] is not None
    ]
    assert impacts == sorted(impacts, reverse=True)


def test_control_browser_compares_full_phases_and_rechecks_cached_evidence(tmp_path):
    record = _create_mock_large_record(tmp_path)
    source = Path(record["before"]["report_artifact"])
    changed = json.loads(source.read_bytes())
    changed["profiles"][0]["controls"][273]["results"] = [{"status": "passed"}]
    after = source.with_name("after.json")
    after.write_text(json.dumps(changed))
    record["after"] = copy.deepcopy(record["before"])
    record["after"].update(
        report_artifact=str(after),
        report_sha256=hashlib.sha256(after.read_bytes()).hexdigest(),
    )
    evidence = ReviewEvidence()
    result = browse_controls(
        tmp_path,
        record,
        ControlQuery(search="control-273", outcome="passed"),
        evidence=evidence,
    )
    assert result["controls"][0]["before"] == "failed"
    assert result["controls"][0]["after"] == "passed"
    assert (
        browse_controls(
            tmp_path, record, ControlQuery(search="control-273", outcome="unresolved")
        )["matched"]
        == 0
    )
    Path(record["before"]["report_artifact"]).write_text("changed")
    with pytest.raises(ValueError, match="SHA-256"):
        browse_controls(tmp_path, record, ControlQuery(), evidence=evidence)


@pytest.mark.parametrize("has_latest_evidence", [False, True])
def test_reassessment_filters_a_previous_pass_as_unassessed(
    tmp_path, has_latest_evidence
):
    record = _create_mock_large_record(tmp_path)
    record["request"].update(action="assess", assessment_of="b" * 32)
    if has_latest_evidence:
        source = Path(record["before"]["report_artifact"])
        changed = json.loads(source.read_bytes())
        del changed["profiles"][0]["controls"][1]
        after = source.with_name("after.json")
        after.write_text(json.dumps(changed))
        record["after"] = {
            **record["before"],
            "report_artifact": str(after),
            "report_sha256": hashlib.sha256(after.read_bytes()).hexdigest(),
        }
    for outcome in ("passed", "unassessed", "unresolved"):
        result = browse_controls(
            tmp_path, record, ControlQuery(search="control-001", outcome=outcome)
        )
        if outcome == "passed":
            assert result["matched"] == 0
        else:
            assert result["matched"] == 1
            assert result["controls"][0]["before"] == "passed"
            assert result["controls"][0]["after"] == "unassessed"
