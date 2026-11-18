"""OpenSCAP execution policy and final controller provenance survive failures."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from lxml import etree

from kerkov.core.compliance.openscap_runtime import _collect, scan_openscap
from kerkov.core.compliance.review import build_report, export_report, review_control
from kerkov.core.compliance.workflows import WorkflowRequest, new_record
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.compliance_scan_tool import ComplianceScanParams

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="OpenSCAP controller requires POSIX"
)


def _create_mock_params():
    return ComplianceScanParams.model_validate(
        {
            "host": "192.0.2.1",
            "user": "audit",
            "profile": "datastream.xml",
            "private_key_file": "key",
            "known_hosts_file": "known_hosts",
            "openscap": {
                "distribution": "Ubuntu",
                "version": "22.04",
                "profile_id": "xccdf_example_profile_server",
                "sha256": "0" * 64,
            },
        }
    )


def test_disabled_runtime_never_prepares_or_executes(tmp_path):
    params = _create_mock_params()
    with patch("kerkov.core.compliance.openscap_runtime._prepare") as prepare:
        result = asyncio.run(
            scan_openscap(tmp_path, params, params.openscap, execution_enabled=False)
        )
    assert not result.success
    prepare.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_collection_uses_arf_benchmark_metadata_for_control_reviews_and_exports(
    tmp_path,
):
    raw = (Path(__file__).parent / "fixtures/openscap-arf.xml").read_bytes()
    root = etree.fromstring(raw)
    result = next(root.iter("{http://checklists.nist.gov/xccdf/1.2}TestResult"))
    profile = result.find("{http://checklists.nist.gov/xccdf/1.2}profile").get("idref")
    params = _create_mock_params()
    settings = params.openscap.model_copy(update={"profile_id": profile})
    params = params.model_copy(update={"openscap": settings})
    artifacts = tmp_path / "artifacts/ansible/run"
    evidence = artifacts / "openscap/selected_asset"
    evidence.mkdir(parents=True)
    (evidence / "results.xml").write_bytes(etree.tostring(result))
    (evidence / "results-arf.xml").write_bytes(raw)
    (evidence / "manifest.json").write_text(
        json.dumps(
            {
                "inventory_alias": "selected_asset",
                "inventory_address": params.host,
                "distribution": settings.distribution,
                "distribution_version": settings.version,
                "content_sha256": settings.sha256,
                "profile_id": profile,
                "scanner_exit_code": 0,
                "scanner_version": "OpenSCAP 1.2.17",
            }
        )
    )

    summary = _collect(tmp_path, params, settings, artifacts)

    assert summary["format"] == "arf"
    assert summary["benchmark_version"] == "0.1.82"
    assert summary["controls"][0]["title"] == "Check account configuration"
    assert Path(summary["report_artifact"]).read_bytes() == raw
    request = WorkflowRequest.model_validate(
        {"action": "assess", "resource_id": 1, "scan": params.model_dump()}
    )
    record = new_record(request, "OpenSCAP assessment")
    record.update(status="passed", before={"engine": "openscap", **summary})
    report = build_report(tmp_path, record)
    control = report["controls"][0]
    assert control["title"] == "Check account configuration"
    review = review_control(tmp_path, record, profile, control["control_id"])
    assert review["before"]["test_count"] == 1
    for format_name in ("html", "json"):
        assert (
            "Check account configuration"
            in export_report(tmp_path, record, format_name)["body"]
        )


@pytest.mark.parametrize(
    "failure",
    [ValueError("bad input"), KeyError("internal bug"), asyncio.CancelledError()],
)
def test_preparation_failures_and_cancellation_write_final_manifest(tmp_path, failure):
    params = _create_mock_params()
    with patch("kerkov.core.compliance.openscap_runtime._prepare", side_effect=failure):
        if isinstance(failure, ValueError):
            result = asyncio.run(
                scan_openscap(tmp_path, params, params.openscap, execution_enabled=True)
            )
            assert not result.success
        else:
            with pytest.raises(type(failure)):
                asyncio.run(
                    scan_openscap(
                        tmp_path, params, params.openscap, execution_enabled=True
                    )
                )
    (manifest,) = tmp_path.glob("artifacts/compliance/openscap-*/manifest.json")
    saved = json.loads(manifest.read_text())
    assert saved["status"] == (
        "cancelled"
        if isinstance(failure, asyncio.CancelledError)
        else "internal_error" if isinstance(failure, KeyError) else "failed"
    )
    assert saved["profile_sha256"] == "0" * 64


def test_cancelled_ansible_execution_keeps_controller_manifest(tmp_path):
    params = _create_mock_params()
    with (
        patch("kerkov.core.compliance.openscap_runtime._prepare", return_value={}),
        patch(
            "kerkov.tools.ansible_tool.AnsibleTool.execute",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                scan_openscap(tmp_path, params, params.openscap, execution_enabled=True)
            )
    (manifest,) = tmp_path.glob("artifacts/compliance/openscap-*/manifest.json")
    assert json.loads(manifest.read_text())["status"] == "cancelled"


@pytest.mark.parametrize("success", [True, False])
def test_execution_result_has_a_final_controller_manifest(tmp_path, success):
    params = _create_mock_params()
    ansible_result = ToolResult(
        success=success,
        output="{}",
        error=None if success else "Ansible execution timed out",
        metadata={"artifact_directory": str(tmp_path / "ansible")},
    )
    with (
        patch("kerkov.core.compliance.openscap_runtime._prepare", return_value={}),
        patch(
            "kerkov.tools.ansible_tool.AnsibleTool.execute",
            new=AsyncMock(return_value=ansible_result),
        ),
        patch(
            "kerkov.core.compliance.openscap_runtime._collect",
            return_value={"controls": [], "notice": "fixture"},
        ),
    ):
        result = asyncio.run(
            scan_openscap(tmp_path, params, params.openscap, execution_enabled=True)
        )
    saved = json.loads(Path(result.metadata["run_manifest_artifact"]).read_text())
    assert saved["status"] == ("completed" if success else "failed")
    assert saved["run_finished_at"] >= saved["run_started_at"]
    assert saved["platform"] == {"distribution": "Ubuntu", "version": "22.04"}
    if not success:
        assert saved["error"] == "Ansible execution timed out"
