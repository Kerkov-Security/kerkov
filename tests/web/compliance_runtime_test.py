"""Browser-session adapters over the shared compliance workflow."""

import hashlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kerkov.core.approval_types import ToolApprovalResult
from kerkov.core.cancellable_task import EventInterrupt
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.workflows import WorkflowRequest, WorkflowStore
from kerkov.core.docker_command import CommandResult
from kerkov.core.knowledge_base.models import LedgerEntry
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.compliance_scan_tool import ComplianceScanParams, ComplianceScanTool
from kerkov.web.runtime.compliance import build_run, list_runs, read_evidence
from kerkov.web.runtime.events import WebEventBus

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Compliance controller requires POSIX"
)


def _create_mock_session(tmp_path):
    row = LedgerEntry(
        id=1,
        engagement_id="root",
        section="assets",
        entry={"kind": "ip", "value": "192.0.2.1"},
    )
    return SimpleNamespace(
        workspace_path=tmp_path,
        inventory=SimpleNamespace(context_id="context"),
        config=SimpleNamespace(
            execution_enabled=True,
            cinc_docker_image="fixture/cinc:7",
            compliance_projects_dir=str(tmp_path / "cache"),
        ),
        findings_ledger=SimpleNamespace(list=AsyncMock(return_value=[row])),
        bus=WebEventBus(),
        approval_strategy=SimpleNamespace(
            approve=AsyncMock(return_value=ToolApprovalResult(approved=True))
        ),
        _closed=False,
        compliance_run=None,
    )


def _create_mock_request(tmp_path, action="preview", **updates):
    key = tmp_path / "key"
    key.write_text("fixture-key")
    key.chmod(0o600)
    known = tmp_path / "known_hosts"
    known.write_text("trusted fixture")
    baseline = builtin_baselines()[0]
    return WorkflowRequest.model_validate(
        {
            "action": action,
            "resource_id": 1,
            "baseline_id": baseline["id"],
            "scan": {
                "host": "192.0.2.1",
                "user": "root",
                "profile": baseline["profile"],
                "private_key_file": str(key),
                "known_hosts_file": str(known),
                "sudo": True,
            },
            **updates,
        }
    )


@pytest.mark.asyncio
async def test_assessment_prompt_omits_unset_arguments_and_matches_plan(tmp_path):
    run = build_run(
        _create_mock_session(tmp_path),
        _create_mock_request(tmp_path, action="assess").model_dump(),
        "assess",
    )
    with (
        patch(
            "kerkov.core.workflows.compliance.shutil.which", return_value="/bin/docker"
        ),
        patch(
            "kerkov.core.workflows.compliance.SubprocessCommandRunner.run",
            new=AsyncMock(return_value=CommandResult(exit_code=0)),
        ),
    ):
        await run.prepare()
    line = next(
        line
        for line in run.plan_message().splitlines()
        if line.startswith("Assessment arguments: ")
    )
    params = json.loads(line.removeprefix("Assessment arguments: "))
    assert "openscap" not in params
    assert "inputs_file" not in params
    decision = await run.approve(SimpleNamespace(name="compliance_scan"), params)
    assert decision.approved
    await run.flush()


@pytest.mark.asyncio
async def test_scap_plan_requires_only_ansible_and_rejects_changed_content(tmp_path):
    session = _create_mock_session(tmp_path)
    original = _create_mock_request(tmp_path, action="assess").model_dump()
    profile = "xccdf_example_profile_server"
    raw = (
        Path(__file__).parents[1] / "core/compliance/fixtures/openscap-datastream.xml"
    ).read_bytes()
    datastream = tmp_path / "datastream.xml"
    datastream.write_bytes(raw)
    original["baseline_id"] = None
    original["scan"].update(
        profile=str(datastream),
        openscap={
            "distribution": "Ubuntu",
            "version": "24.04",
            "profile_id": profile,
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    request = WorkflowRequest.model_validate(original)
    with pytest.raises(ValueError, match="assessment only"):
        build_run(
            session,
            request.model_copy(update={"action": "apply"}).model_dump(),
            "apply",
        )
    run = build_run(session, request.model_dump(), "assess")
    with (
        patch(
            "kerkov.core.workflows.compliance.shutil.which", side_effect=AssertionError
        ),
        patch(
            "kerkov.core.workflows.compliance.ansible_binary",
            return_value="/bin/ansible-playbook",
        ),
    ):
        await run.prepare()
    snapshot = Path(run.scan["profile"])
    assert snapshot != datastream and snapshot.read_bytes() == raw
    assert run.record["plan"]["engine"] == "openscap"
    assert "compliance_scan" in run.plan_message()
    snapshot.write_bytes(b"changed")
    result = await run.approve(ComplianceScanTool(config=session.config), run.scan)
    assert not result.approved
    session.approval_strategy.approve.assert_not_awaited()
    from kerkov.core.compliance.openscap import assessment_controls, parse_report

    parsed = parse_report(
        (
            Path(__file__).parents[1] / "core/compliance/fixtures/results.xml"
        ).read_bytes()
    )
    summary, _ = assessment_controls(parsed)
    assert "profile_error_count" not in summary
    data = {
        **summary,
        "engine": "openscap",
        "status": "completed",
        "evidence_status": "scanned",
        "profile_name": profile,
        "profile_sha256": original["scan"]["openscap"]["sha256"],
        "artifact_directory": str(tmp_path / "artifacts/compliance/fixture"),
        "target": {key: run.scan[key] for key in ("host", "user", "port")},
        "sudo": run.scan["sudo"],
    }
    run.pending = "compliance_scan"
    run.result(
        "compliance_scan",
        ToolResult(
            success=True, output=json.dumps(data), metadata={"status": "completed"}
        ),
    )
    assert run.assessment_count == 1
    assert not run.failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"pass": 1, "notselected": 499}, "passed"),
        ({"pass": 1, "notapplicable": 1, "informational": 1}, "attention"),
        ({"notselected": 500}, "attention"),
        *[
            ({"pass": 1, status: 1}, "attention")
            for status in (
                "fail",
                "error",
                "unknown",
                "notchecked",
                "fixed",
                "waived",
                "notapplicable",
                "informational",
            )
        ],
    ],
)
async def test_scap_pass_is_scoped_to_evaluated_rules(tmp_path, counts, expected):
    request = _create_mock_request(tmp_path, action="assess", baseline_id=None)
    request.scan.openscap = ComplianceScanParams.model_validate(
        {
            **request.scan.model_dump(),
            "openscap": {
                "distribution": "Ubuntu",
                "version": "22.04",
                "profile_id": "xccdf_example_profile_server",
                "sha256": "0" * 64,
            },
        }
    ).openscap
    run = build_run(_create_mock_session(tmp_path), request.model_dump(), "scap")
    run.assessment_count = 1
    run.record["before"] = {"xccdf_counts": counts}
    await run.finish(EventInterrupt())
    assert run.record["status"] == expected
    if expected == "passed":
        assert "outside the selected profile are not passes" in run.record["detail"]


def _create_mock_assessment(run, status="failed"):
    controls = [
        {
            "profile": run.baseline["profile_name"],
            "control_id": name,
            "title": name,
            "impact": 0.7,
            "status": status,
        }
        for name in run.baseline["controls"]
    ]
    counts = dict.fromkeys(
        ["passed", "failed", "error", "skipped", "waived", "no_tests"], 0
    )
    counts[status] = len(controls)
    data = {
        "status": "completed",
        "evidence_status": "scanned",
        "profile_sha256": run.record["plan"]["files"]["profile_sha256"],
        "profile_name": run.baseline["profile_name"],
        "profile_error_count": 0,
        "artifact_directory": str(run.workspace / "artifacts/compliance/scan-fixture"),
        "target": {key: run.scan[key] for key in ["host", "user", "port"]},
        "sudo": True,
        "control_count": len(controls),
        "counts": counts,
        "controls": controls,
        "has_inconclusive_results": status
        in {"error", "skipped", "waived", "no_tests"},
    }
    return ToolResult(success=True, output=json.dumps(data), metadata=data)


@pytest.mark.asyncio
async def test_reconnect_projects_unknown_run_and_evidence_stays_confined(tmp_path):
    session = _create_mock_session(tmp_path)
    run = build_run(
        session, _create_mock_request(tmp_path, "assess").model_dump(), "assess"
    )
    run.publish()
    await run.flush()
    session.bus.drain_nowait()
    await list_runs(session, {"context_id": "context"})
    record = session.bus.drain_nowait()[-1]["payload"]["runs"][0]
    assert record["status"] == "interrupted"
    assert "reassess" in record["detail"].lower()
    outside = tmp_path.parent / "outside-evidence.json"
    outside.write_text("do not expose")
    record["before"] = {"normalized_artifact": str(outside)}
    WorkflowStore(tmp_path).save(record)
    await read_evidence(
        session, {"context_id": "context", "run_id": record["id"], "phase": "before"}
    )
    assert "do not expose" not in session.bus.drain_nowait()[-1]["payload"]["body"]
    await list_runs(session, {"context_id": "old"})
    assert session.bus.drain_nowait() == []


@pytest.mark.asyncio
async def test_structured_workflow_uses_shared_engine_tools_and_history(tmp_path):
    from kerkov.core.knowledge_base.models import LedgerSection
    from kerkov.core.run_workspace import get_current_workspace
    from kerkov.core.session.approval_mode_manager import ApprovalMode
    from kerkov.models.basic_models import LLMResponse, ToolCall
    from kerkov.web.runtime.session import WebKerkovSession
    from tests.web.web_runtime_test import _config, _FakeProvider

    bus = WebEventBus()
    provider = _FakeProvider([])
    session = WebKerkovSession(
        bus, config=_config(tmp_path, execution_enabled=True), provider=provider
    )
    await session.initialize()
    session._apply_system_prompt_if_needed()
    asset_id = await session.findings_ledger.add(
        LedgerSection.ASSETS,
        {"kind": "ip", "value": "192.0.2.1"},
        engagement_id=session.engagement_store.engagement_id,
    )
    session.approval_mode_manager.set(ApprovalMode.PERMISSIVE)
    request = _create_mock_request(tmp_path).model_copy(
        update={"resource_id": asset_id}
    )
    calls = []

    async def complete(messages, **kwargs):
        run = session.compliance_run
        assert kwargs["tools"] == session.tools
        if not run.assessment_count:
            name, params = "compliance_scan", run.scan
        elif run.record["remediation"] is None:
            name, params = "ansible", run.remediation
        else:
            return LLMResponse(content="Preview completed", model="fake-model")
        return LLMResponse(
            content="",
            model="fake-model",
            tool_calls=[ToolCall(tool_name=name, parameters=params, call_id=name)],
        )

    async def scan(tool, params):
        assert get_current_workspace() == session.workspace_path
        calls.append("scan")
        return _create_mock_assessment(session.compliance_run)

    async def ansible(tool, params):
        assert params.check is True
        calls.append("preview")
        return ToolResult(
            success=True,
            output="{}",
            metadata={
                "check": True,
                "bootstrap_python": params.bootstrap_python,
                "totals": {"changed": 1},
                "changed_tasks": [
                    {"host": "selected_asset", "task": "Set permissions"}
                ],
            },
        )

    try:
        provider.complete = AsyncMock(side_effect=complete)
        with (
            patch(
                "kerkov.core.workflows.compliance.shutil.which",
                return_value="/bin/docker",
            ),
            patch(
                "kerkov.core.workflows.compliance.ansible_binary",
                return_value="/bin/ansible-playbook",
            ),
            patch(
                "kerkov.core.workflows.compliance.SubprocessCommandRunner.run",
                new=AsyncMock(return_value=CommandResult(exit_code=0)),
            ),
            patch("kerkov.tools.compliance_scan_tool.ComplianceScanTool.run", new=scan),
            patch("kerkov.tools.ansible_tool.AnsibleTool.run", new=ansible),
        ):
            await session.handle_user_message(
                "Run selected compliance plan",
                workflow_id="workflow",
                workflow_request=request.model_dump(),
            )
        record = WorkflowStore(session.workspace_path).list()[0]
        assert record["status"] == "previewed"
        assert record["remediation"]["changed_tasks"] == [
            {"host": "selected_asset", "task": "Set permissions"}
        ]
        assert calls == ["scan", "preview"]
        assert any(
            m.role == "tool" and m.context_only for m in session.conversation_history
        )
        events = bus.drain_nowait()
        assert any(e["type"] == "compliance.updated" for e in events)
        assert not any(e["type"] == "assistant.message" for e in events)
    finally:
        await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "execution_disabled",
        "unknown_baseline",
        "invalid_request",
        "storage",
        "no_engagement",
    ],
)
async def test_workflow_readiness_errors_are_specific_and_cleanup_is_safe(
    tmp_path, failure
):
    from kerkov.web.runtime.session import WebKerkovSession
    from tests.web.web_runtime_test import _config, _FakeProvider

    bus = WebEventBus()
    session = WebKerkovSession(
        bus, config=_config(tmp_path), provider=_FakeProvider([])
    )
    await session.initialize()
    session._apply_system_prompt_if_needed()
    if failure == "no_engagement":
        session.workspace_path = None
    request = _create_mock_request(tmp_path).model_dump()
    if failure == "unknown_baseline":
        request["baseline_id"] = "missing"
    elif failure == "invalid_request":
        request["resource_id"] = "sentinel-secret"
    with (
        patch(
            "kerkov.core.workflows.compliance.WorkflowStore.save",
            side_effect=OSError("disk full"),
        )
        if failure == "storage"
        else nullcontext()
    ):
        try:
            await session.handle_user_message(
                "Run selected compliance plan",
                workflow_id="workflow",
                workflow_request=request,
            )
            events = bus.drain_nowait()
            errors = [
                e["payload"]["payload"]["detail"]
                for e in events
                if e["type"] == "compliance.event"
                and e["payload"]["event_type"] == "error"
            ]
            assert errors
            expected = {
                "unknown_baseline": "Unknown compliance baseline",
                "invalid_request": "Invalid compliance settings",
                "execution_disabled": "EXECUTION_ENABLED",
                "storage": "EXECUTION_ENABLED",
                "no_engagement": "Open an engagement",
            }[failure]
            assert expected in errors[0]
            assert "sentinel-secret" not in errors[0]
            assert session.compliance_run is None
        finally:
            await session.aclose()
