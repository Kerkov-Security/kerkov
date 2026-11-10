"""Local evidence reviews and complete, portable assessment exports."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.compliance.cinc_report import decode_report, parse_report
from kerkov.core.compliance.review import (
    ReviewEvidence,
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
