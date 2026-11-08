"""Durable compliance plans, execution constraints and reconnect projections."""

import json
import shlex
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kerkov.core.approval_types import ToolApprovalResult
from kerkov.core.automation.profiles import ProfileStore
from kerkov.core.cancellable_task import EventInterrupt
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.workflows import WorkflowRequest, WorkflowStore
from kerkov.core.docker_command import CommandResult
from kerkov.core.knowledge_base.models import LedgerEntry
from kerkov.tools.ansible_tool import AnsibleParams
from kerkov.tools.base_tool import ToolResult
from kerkov.web.runtime.compliance import ComplianceRun, list_runs, read_evidence
from kerkov.web.runtime.events import WebEventBus
from tests.core.compliance.profile_bundle_test import _create_mock_bundle

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
            execution_enabled=True, cinc_docker_image="fixture/cinc:7"
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


async def _prepare(run):
    with (
        patch("kerkov.web.runtime.compliance.shutil.which", return_value="/bin/docker"),
        patch(
            "kerkov.web.runtime.compliance.ansible_binary",
            return_value="/bin/ansible-playbook",
        ),
        patch(
            "kerkov.web.runtime.compliance.SubprocessCommandRunner.run",
            new=AsyncMock(return_value=CommandResult(exit_code=0)),
        ),
    ):
        await run.prepare()


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


async def _assess(run, status="failed"):
    params = dict(run.scan)
    if run.assessment_count:
        params["profile"] = run.record["before"]["artifact_directory"] + "/profile"
    assert (await run.approve(SimpleNamespace(name="compliance_scan"), params)).approved
    result = _create_mock_assessment(run, status)
    snapshot = Path(result.metadata["artifact_directory"]) / "profile"
    if not snapshot.exists():
        shutil.copytree(run.scan["profile"], snapshot)
    run.result("compliance_scan", result)


@pytest.mark.asyncio
async def test_existing_project_plan_accepts_dependencies_without_remediation(tmp_path):
    project = tmp_path / "external"
    _create_mock_bundle(project)
    request = _create_mock_request(tmp_path, action="assess", baseline_id=None)
    request.scan.profile = str(project)
    run = ComplianceRun(_create_mock_session(tmp_path), request, "external")
    await _prepare(run)
    assert run.record["label"] == "upstream-project"
    assert run.record["plan"]["profile_bundle"]["dependencies"][0]["name"] == "shared"
    assert run.remediation is None
    assert "Assessment only" in run.record["plan"]["changes"]
    snapshot = Path(run.scan["profile"])
    (snapshot / "vendor" / ("1" * 64) / "libraries/shared.rb").write_text("changed")
    decision = await run.approve(SimpleNamespace(name="compliance_scan"), run.scan)
    assert not decision.approved
    await run.flush()


@pytest.mark.asyncio
async def test_preview_apply_and_verification_are_saved_and_compared(tmp_path):
    session = _create_mock_session(tmp_path)
    preview = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(preview)
    await _assess(preview)
    assert (
        await preview.approve(SimpleNamespace(name="ansible"), preview.remediation)
    ).approved
    preview.result(
        "ansible", ToolResult(success=True, output="", metadata={"check": True})
    )
    await preview.finish(EventInterrupt())
    assert WorkflowStore(tmp_path).load(preview.record["id"])["status"] == "previewed"

    apply = ComplianceRun(
        session,
        _create_mock_request(tmp_path, "apply", preview_id=preview.record["id"]),
        "apply",
    )
    await _prepare(apply)
    assert apply.remediation["check"] is False
    await _assess(apply)
    assert (
        await apply.approve(SimpleNamespace(name="ansible"), apply.remediation)
    ).approved
    apply.result(
        "ansible", ToolResult(success=True, output="", metadata={"check": False})
    )
    await _assess(apply, "passed")
    await apply.finish(EventInterrupt())
    saved = WorkflowStore(tmp_path).load(apply.record["id"])
    assert saved["status"] == "verified"
    assert all(
        c["before"] == "failed" and c["after"] == "passed"
        for c in apply.projection()["controls"]
    )
    assert session.approval_strategy.approve.await_count == 5
    assert not any(
        e["type"].startswith("assistant.") for e in session.bus.drain_nowait()
    )


@pytest.mark.asyncio
async def test_unsafe_sequence_arguments_and_preview_mismatch_are_rejected(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    tool = SimpleNamespace(name="ansible")
    assert not (await run.approve(tool, run.remediation)).approved
    assert session.approval_strategy.approve.await_count == 0
    wrong_host = {**run.scan, "host": "192.0.2.2"}
    assert not (
        await run.approve(SimpleNamespace(name="compliance_scan"), wrong_host)
    ).approved
    assert not (await run.approve(SimpleNamespace(name="exec"), {})).approved
    await run.finish(EventInterrupt())
    assert run.record["status"] == "incomplete"
    apply = ComplianceRun(
        session,
        _create_mock_request(tmp_path, "apply", preview_id=run.record["id"]),
        "apply",
    )
    with pytest.raises(ValueError, match="preview does not match"):
        await _prepare(apply)


@pytest.mark.asyncio
async def test_changed_plan_files_fail_before_execution(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    Path(run.scan["known_hosts_file"]).write_text("changed trust")
    assert not (
        await run.approve(SimpleNamespace(name="compliance_scan"), run.scan)
    ).approved
    assert session.approval_strategy.approve.await_count == 0
    await run.finish(EventInterrupt())


@pytest.mark.asyncio
async def test_older_saved_baseline_can_be_reassessed_without_remediation_mapping(
    tmp_path,
):
    session = _create_mock_session(tmp_path)
    original = ComplianceRun(
        session, _create_mock_request(tmp_path, "assess"), "original"
    )
    await _prepare(original)
    await _assess(original)
    await original.finish(EventInterrupt())
    original.record["plan"]["baseline"].pop("remediation")
    original.store.save(original.record)
    recovered = ComplianceRun(
        session,
        _create_mock_request(tmp_path, "assess", assessment_of=original.record["id"]),
        "reassessment",
    )
    await _prepare(recovered)
    assert recovered.record["plan"]["baseline"]["remediation"] == {}
    await recovered.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "error", "skipped", "waived", "no_tests"])
async def test_nonpassing_assessments_never_claim_baseline_passed(tmp_path, status):
    run = ComplianceRun(
        _create_mock_session(tmp_path),
        _create_mock_request(tmp_path, "assess"),
        "assess",
    )
    await _prepare(run)
    await _assess(run, status)
    await run.finish(EventInterrupt())
    assert run.record["status"] == "attention"


@pytest.mark.asyncio
async def test_partial_failure_can_be_reassessed_but_never_retried_or_verified(
    tmp_path,
):
    run = ComplianceRun(
        _create_mock_session(tmp_path), _create_mock_request(tmp_path), "preview"
    )
    await _prepare(run)
    await _assess(run)
    assert (
        await run.approve(SimpleNamespace(name="ansible"), run.remediation)
    ).approved
    run.result("ansible", ToolResult(success=False, output="", error="Partial changes"))
    assert not (
        await run.approve(SimpleNamespace(name="ansible"), run.remediation)
    ).approved
    await run.finish(EventInterrupt())
    assert run.record["status"] == "failed"
    assert run.record["detail"] == "Partial changes"


@pytest.mark.asyncio
async def test_reconnect_projects_unknown_run_and_evidence_stays_confined(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path, "assess"), "assess")
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
async def test_execution_disabled_does_not_touch_docker_or_plan_files(tmp_path):
    session = _create_mock_session(tmp_path)
    session.config.execution_enabled = False
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    with patch(
        "kerkov.web.runtime.compliance.SubprocessCommandRunner.run", new=AsyncMock()
    ) as docker:
        with pytest.raises(ValueError, match="EXECUTION_ENABLED"):
            await run.prepare()
    docker.assert_not_called()
    assert not run.store.root.exists()


@pytest.mark.asyncio
async def test_reassessment_uses_original_profile_and_preserves_the_prior_run(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path, "assess"), "first")
    await _prepare(run)
    await _assess(run)
    await run.finish(EventInterrupt())
    request = run.request.model_copy(update={"assessment_of": run.record["id"]})
    reassess = ComplianceRun(session, request, "reassessment")
    await _prepare(reassess)
    await _assess(reassess, "passed")
    await reassess.finish(EventInterrupt())
    assert reassess.record["status"] == "passed"
    assert all(
        c["before"] == "failed" and c["after"] == "passed"
        for c in reassess.projection()["controls"]
    )
    assert WorkflowStore(tmp_path).load(run.record["id"])["status"] == "attention"


@pytest.mark.asyncio
async def test_approval_wait_rechecks_asset_before_execution(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path, "assess"), "assess")
    await _prepare(run)

    async def approve(tool, params):
        session.findings_ledger.list.return_value = []
        return ToolApprovalResult(approved=True)

    session.approval_strategy.approve.side_effect = approve
    assert not (
        await run.approve(SimpleNamespace(name="compliance_scan"), run.scan)
    ).approved
    await run.finish(EventInterrupt())
    assert run.record["status"] == "blocked"


@pytest.mark.asyncio
async def test_plan_snapshot_cannot_bypass_artifact_profile_validation(tmp_path):
    session = _create_mock_session(tmp_path)
    request = _create_mock_request(tmp_path, "assess")
    source = tmp_path / "artifacts/untrusted"
    source.mkdir(parents=True)
    (source / "inspec.yml").write_text("name: untrusted")
    request.baseline_id = None
    request.scan.profile = str(source)
    run = ComplianceRun(session, request, "assess")
    with pytest.raises(ValueError, match="artifact subtree"):
        await _prepare(run)


@pytest.mark.asyncio
async def test_generated_inventory_keeps_explicit_ssh_trust_with_quoted_paths(tmp_path):
    request = _create_mock_request(tmp_path)
    trust = tmp_path / "trusted ' host keys"
    trust.write_text("fixture trust")
    request.scan.known_hosts_file = str(trust)
    run = ComplianceRun(_create_mock_session(tmp_path), request, "preview")
    await _prepare(run)
    inventory = json.loads(Path(run.remediation["inventory"]).read_text())
    assert set(inventory["all"]["hosts"]) == {"selected_asset"}
    host = inventory["all"]["hosts"]["selected_asset"]
    args = shlex.split(host["ansible_ssh_common_args"])
    assert "StrictHostKeyChecking=yes" in args
    assert "IdentitiesOnly=yes" in args
    assert f"UserKnownHostsFile={json.dumps(str(trust))}" in args
    assert Path(run.remediation["playbook"]).is_relative_to(Path(run.scan["profile"]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["imported", "changed_hash", "wrong_host", "missing_counts", "wrong_control"],
)
async def test_only_matching_scanned_evidence_can_complete_an_assessment(
    tmp_path, change
):
    run = ComplianceRun(
        _create_mock_session(tmp_path),
        _create_mock_request(tmp_path, "assess"),
        "assess",
    )
    await _prepare(run)
    assert (
        await run.approve(SimpleNamespace(name="compliance_scan"), run.scan)
    ).approved
    data = json.loads(_create_mock_assessment(run).output)
    if change == "imported":
        data["evidence_status"] = "imported"
    elif change == "changed_hash":
        data["profile_sha256"] = "b" * 64
    elif change == "wrong_host":
        data["target"]["host"] = "192.0.2.2"
    elif change == "missing_counts":
        del data["counts"]
    else:
        data["controls"][0]["control_id"] = "unrelated-control"
    run.result(
        "compliance_scan",
        ToolResult(success=True, output=json.dumps(data), metadata=data),
    )
    await run.finish(EventInterrupt())
    assert run.record["status"] == "failed"
    assert run.record["before"] is None


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
            metadata={"check": True, "bootstrap_python": params.bootstrap_python},
        )

    try:
        provider.complete = AsyncMock(side_effect=complete)
        with (
            patch(
                "kerkov.web.runtime.compliance.shutil.which", return_value="/bin/docker"
            ),
            patch(
                "kerkov.web.runtime.compliance.ansible_binary",
                return_value="/bin/ansible-playbook",
            ),
            patch(
                "kerkov.web.runtime.compliance.SubprocessCommandRunner.run",
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
async def test_custom_remediation_keeps_selected_alias_and_groups(tmp_path):
    session = _create_mock_session(tmp_path)
    request = _create_mock_request(tmp_path)
    request.baseline_id = None
    playbook = tmp_path / "remediate.yml"
    playbook.write_text("- hosts: webservers\n  tasks: []\n")
    template = tmp_path / "profile.json"
    template.write_text(
        json.dumps(
            {
                "name": "custom",
                "hosts": [
                    {
                        "resource_id": 1,
                        "alias": "web1",
                        "groups": ["webservers"],
                        "user": "root",
                        "private_key_file": request.scan.private_key_file,
                    },
                    {
                        "resource_id": 2,
                        "alias": "web2",
                        "groups": ["webservers", "other"],
                        "user": "root",
                        "private_key_file": request.scan.private_key_file,
                    },
                ],
                "playbooks": {"fix": str(playbook)},
            }
        )
    )
    rows = await session.findings_ledger.list()
    rows.append(
        LedgerEntry(
            id=2,
            engagement_id="root",
            section="assets",
            entry={"kind": "ip", "value": "192.0.2.2"},
        )
    )
    ProfileStore(tmp_path).create(template, rows)
    request.remediation = AnsibleParams(profile="custom", playbook="fix", limit="web1")
    run = ComplianceRun(session, request, "preview")
    await _prepare(run)
    assert 'Saved Ansible profile for inspection: "custom"' in run.prompt()
    inspection = AnsibleParams(action="profile", profile="custom").model_dump()
    assert (await run.approve(SimpleNamespace(name="ansible"), inspection)).approved
    run.result(
        "ansible",
        ToolResult(success=True, output="profile", metadata={"action": "profile"}),
    )
    assert not run.remediation_attempted
    assert run.record["remediation"] is None
    assert not (
        await run.approve(
            SimpleNamespace(name="ansible"), {**inspection, "profile": "other"}
        )
    ).approved
    assert not run.blocked
    inventory = json.loads(Path(run.remediation["inventory"]).read_text())
    assert run.remediation["limit"] == "web1"
    assert set(inventory["all"]["hosts"]) == {"web1"}
    assert inventory["all"]["children"] == {"webservers": {"hosts": {"web1": {}}}}
    assert (
        "StrictHostKeyChecking=yes"
        in inventory["all"]["hosts"]["web1"]["ansible_ssh_common_args"]
    )
    await run.finish(EventInterrupt())


@pytest.mark.asyncio
@pytest.mark.parametrize("during_approval", [False, True])
@pytest.mark.parametrize(
    "change", ["control", "library", "removed", "symlink", "inventory"]
)
async def test_reviewed_files_rechecked_before_execution(
    tmp_path, during_approval, change
):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)

    def mutate():
        profile = Path(run.scan["profile"])
        control = next((profile / "controls").glob("*.rb"))
        if change == "control":
            control.write_text("changed control")
        elif change == "library":
            library = profile / "libraries" / "new.rb"
            library.parent.mkdir(exist_ok=True)
            library.write_text("changed library")
        elif change == "removed":
            control.unlink()
        elif change == "symlink":
            (profile / "linked").symlink_to(control)
        else:
            Path(run.remediation["inventory"]).write_text("{}")

    async def approve(tool, params):
        mutate()
        return ToolApprovalResult(approved=True)

    if during_approval:
        session.approval_strategy.approve.side_effect = approve
    else:
        mutate()
    assert not (
        await run.approve(SimpleNamespace(name="compliance_scan"), run.scan)
    ).approved
    assert session.approval_strategy.approve.await_count == int(during_approval)
    await run.finish(EventInterrupt())


@pytest.mark.asyncio
async def test_verification_checks_actual_artifact_profile(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    await _assess(run)
    run.request.action = "apply"
    run.remediation_attempted = True
    profile = Path(run.record["before"]["artifact_directory"]) / "profile"
    next((profile / "controls").glob("*.rb")).write_text("changed verification")
    params = {**run.scan, "profile": str(profile)}
    assert not (
        await run.approve(SimpleNamespace(name="compliance_scan"), params)
    ).approved
    assert session.approval_strategy.approve.await_count == 1
    await run.finish(EventInterrupt())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial", ["outside_read", "invalid_scan", "ledger", "inspection", "exec"]
)
async def test_harmless_denials_allow_planned_remediation(tmp_path, denial):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    await _assess(run)
    tool, params = {
        "outside_read": ("read", {"file_path": str(tmp_path / "other.yml")}),
        "invalid_scan": ("compliance_scan", {"host": "wrong"}),
        "ledger": ("ledger", {"op": "list"}),
        "inspection": ("ansible", {"action": "profile", "profile": "other"}),
        "exec": ("exec", {"command": "true"}),
    }[denial]
    session.approval_strategy.approve.return_value = ToolApprovalResult(approved=False)
    assert not (await run.approve(SimpleNamespace(name=tool), params)).approved
    assert not run.blocked
    session.approval_strategy.approve.return_value = ToolApprovalResult(approved=True)
    assert (
        await run.approve(SimpleNamespace(name="ansible"), run.remediation)
    ).approved
    run.result("ansible", ToolResult(success=True, output="{}"))
    await run.finish(EventInterrupt())
    assert run.record["status"] == "previewed"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["compliance_scan", "ansible"])
async def test_operator_execution_denial_blocks_remediation(tmp_path, tool_name):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    if tool_name == "ansible":
        await _assess(run)
    session.approval_strategy.approve.return_value = ToolApprovalResult(approved=False)
    params = run.scan if tool_name == "compliance_scan" else run.remediation
    assert not (await run.approve(SimpleNamespace(name=tool_name), params)).approved
    assert run.blocked
    assert not (
        await run.approve(SimpleNamespace(name="ansible"), run.remediation)
    ).approved
    await run.finish(EventInterrupt())
    assert run.record["status"] == "blocked"


@pytest.mark.asyncio
async def test_rejected_read_after_verification_does_not_replace_verified_outcome(
    tmp_path,
):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    await _assess(run)
    run.request.action = "apply"
    run.remediation["check"] = False
    assert (
        await run.approve(SimpleNamespace(name="ansible"), run.remediation)
    ).approved
    run.result("ansible", ToolResult(success=True, output="{}"))
    await _assess(run, "passed")
    assert not (
        await run.approve(SimpleNamespace(name="read"), {"file_path": "/outside"})
    ).approved
    await run.finish(EventInterrupt())
    assert run.record["status"] == "verified"


@pytest.mark.asyncio
async def test_storage_failure_keeps_tool_result_and_retries_final_record(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    assert (
        await run.approve(SimpleNamespace(name="compliance_scan"), run.scan)
    ).approved
    result = _create_mock_assessment(run)
    with patch.object(run.store, "save", side_effect=OSError("disk full")):
        run.output.tool_succeeded("compliance_scan", result)
        await run.flush()
        assert run.record["before"] is not None
        assert run.blocked
        assert any(
            e["type"] == "compliance.event"
            and e["payload"]["event_type"] == "tool.result"
            for e in session.bus.drain_nowait()
        )
        assert not (
            await run.approve(SimpleNamespace(name="ansible"), run.remediation)
        ).approved
        await run.finish(EventInterrupt())
        assert run.projection()["persistence_error"]
    await run.finish(EventInterrupt())
    saved = run.store.load(run.record["id"])
    assert saved["before"] == run.record["before"]
    assert saved["status"] == "blocked"
    assert "persistence_error" not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("write_number", [1, 2])
async def test_approval_write_failure_never_returns_execution_permission(
    tmp_path, write_number
):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path), "preview")
    await _prepare(run)
    await _assess(run)
    save = run.store.save
    writes = 0

    def save_or_fail(record):
        nonlocal writes
        writes += 1
        if writes == write_number:
            raise OSError("disk full")
        save(record)

    with patch.object(run.store, "save", side_effect=save_or_fail):
        assert not (
            await run.approve(SimpleNamespace(name="ansible"), run.remediation)
        ).approved
    assert not run.remediation_attempted
    assert run.pending is None
    await run.finish(EventInterrupt())


@pytest.mark.asyncio
async def test_reassessment_without_prepared_plan_has_actionable_error(tmp_path):
    session = _create_mock_session(tmp_path)
    request = _create_mock_request(tmp_path, "assess")
    original = ComplianceRun(session, request, "original")
    original.failed = True
    await original.finish(EventInterrupt())
    recovery = ComplianceRun(
        session,
        request.model_copy(update={"assessment_of": original.record["id"]}),
        "recovery",
    )
    with pytest.raises(ValueError, match="no prepared profile"):
        await _prepare(recovery)


@pytest.mark.asyncio
async def test_reassessment_ignores_runtime_and_display_limits(tmp_path):
    session = _create_mock_session(tmp_path)
    run = ComplianceRun(session, _create_mock_request(tmp_path, "assess"), "first")
    await _prepare(run)
    await _assess(run)
    await run.finish(EventInterrupt())
    request = run.request.model_copy(
        deep=True, update={"assessment_of": run.record["id"]}
    )
    request.scan.timeout_seconds += 1
    request.scan.max_controls += 1
    recovery = ComplianceRun(session, request, "recovery")
    await _prepare(recovery)
    await _assess(recovery, "passed")
    await recovery.finish(EventInterrupt())
    assert recovery.record["status"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_python_bootstrap_requires_explicit_matching_preview(tmp_path, enabled):
    session = _create_mock_session(tmp_path)
    request = _create_mock_request(tmp_path, bootstrap_python=enabled)
    run = ComplianceRun(session, request, "preview")
    await _prepare(run)
    assert run.remediation["bootstrap_python"] is enabled
    await _assess(run)
    assert (
        await run.approve(SimpleNamespace(name="ansible"), run.remediation)
    ).approved
    run.result("ansible", ToolResult(success=True, output="{}"))
    await run.finish(EventInterrupt())
    apply_request = request.model_copy(
        update={
            "action": "apply",
            "preview_id": run.record["id"],
            "bootstrap_python": not enabled,
        }
    )
    apply = ComplianceRun(session, apply_request, "apply")
    with pytest.raises(ValueError, match="preview does not match"):
        await _prepare(apply)


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
            "kerkov.web.runtime.compliance.WorkflowStore.save",
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
