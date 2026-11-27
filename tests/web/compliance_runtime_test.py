"""Browser-session adapters over the shared compliance workflow."""

import asyncio
import hashlib
import json
import shlex
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from kerkov.core.approval_types import ToolApprovalResult
from kerkov.core.cancellable_task import EventInterrupt, InterruptedByUser
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.custom_profiles import CustomProfile, CustomProfileStore
from kerkov.core.compliance.recommendation import OS_RELEASE_PROBE
from kerkov.core.compliance.scap_catalog import ScapPreset
from kerkov.core.compliance.scap_modules import SCAP_MODULES, ScapModule
from kerkov.core.compliance.workflows import WorkflowRequest, WorkflowStore
from kerkov.core.docker_command import CommandResult
from kerkov.core.knowledge_base.models import LedgerEntry
from kerkov.core.session.modes import ModeType
from kerkov.tools.base_tool import ToolResult
from kerkov.tools.compliance_scan_tool import ComplianceScanParams, ComplianceScanTool
from kerkov.web.runtime.compliance import (
    build_run,
    list_runs,
    read_evidence,
    recommend_for_host,
)
from kerkov.web.runtime.events import WebEventBus, WorkflowEventBus

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
async def test_custom_profile_run_pins_saved_checks_and_reassesses_snapshot(tmp_path):
    session = _create_mock_session(tmp_path)
    store = CustomProfileStore(Path(session.config.compliance_projects_dir))
    draft = CustomProfile.model_validate(
        {
            "name": "Account check",
            "checks": [
                {"kind": "file_mode", "target": "/etc/passwd", "expected": "0644"}
            ],
        }
    )
    saved = store.save(draft, None)
    request = _create_mock_request(
        tmp_path, action="assess", baseline_id=None
    ).model_dump()
    request["custom_profile"] = {"id": saved["id"], "revision": saved["revision"]}
    request["scan"]["profile"] = saved["id"]
    with (
        patch(
            "kerkov.core.workflows.compliance.shutil.which", return_value="/bin/docker"
        ),
        patch(
            "kerkov.core.workflows.compliance.SubprocessCommandRunner.run",
            new=AsyncMock(return_value=CommandResult(exit_code=0)),
        ),
    ):
        with pytest.raises(ValueError, match="assessment only"):
            build_run(session, {**request, "action": "preview"}, "preview")
        run = build_run(session, request, "scan")
        await run.prepare()
        await run.flush()
        assert run.record["label"] == "Account check"
        original = (Path(run.scan["profile"]) / "controls/checks.rb").read_bytes()
        assert b"cmp '0644'" in original
        store.save(
            draft.model_copy(update={"name": "New title"}), run.request.custom_profile
        )
        with pytest.raises(ValueError, match="profile changed"):
            await build_run(session, request, "stale").prepare()
        retry = build_run(
            session, {**request, "assessment_of": run.record["id"]}, "retry"
        )
        await retry.prepare()
        await retry.flush()
        assert retry.record["label"] == "Account check"
        assert (
            Path(retry.scan["profile"]) / "controls/checks.rb"
        ).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal", "command_output", "profile_id"),
    [
        (
            "stig",
            "KERKOV_OS_RELEASE=Linux\nID=ubuntu\nVERSION_ID=24.04",
            "mitre-ubuntu-2404-stig",
        ),
        (
            "pci",
            "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
            "KERKOV_OS_RELEASE=Linux\nID=rhel\nVERSION_ID=9.9",
            "pci-dss-rhel9-ssg-0.1.82",
        ),
    ],
)
async def test_auto_detection_uses_shared_tool_approval_on_bound_endpoint(
    tmp_path, goal, command_output, profile_id
):
    session = _create_mock_session(tmp_path)
    request = _create_mock_request(tmp_path, action="assess").model_dump()["scan"]
    request.update(port=2222, profile="auto")
    session.tools = [SimpleNamespace(name="exec")]
    session.engine = SimpleNamespace(
        execute_tool_call=AsyncMock(
            return_value=(
                ToolApprovalResult(approved=True),
                ToolResult(
                    success=True,
                    output=command_output,
                    metadata={"exit_code": 0},
                ),
            )
        )
    )
    bus = WorkflowEventBus(session.bus, "detect")
    content = {"action": "recommend", "resource_id": 1, "goal": goal, "scan": request}
    await recommend_for_host(session, content, bus, EventInterrupt(), "detect")
    call = session.engine.execute_tool_call.await_args
    assert call.kwargs["approval"] is session.approval_strategy
    assert call.kwargs["stop_sensitive"] is True
    argv = shlex.split(call.args[1].parameters["command"])
    assert argv[0] == "ssh" and "StrictHostKeyChecking=yes" in argv
    assert argv[-5:] == [
        "-p",
        "2222",
        "--",
        "192.0.2.1",
        OS_RELEASE_PROBE,
    ]
    (event,) = session.bus.drain_nowait()
    assert event["payload"]["event_type"] == "compliance.recommended"
    assert event["payload"]["payload"]["id"] == profile_id
    with pytest.raises(ValueError, match="host changed"):
        await recommend_for_host(
            session, {**content, "resource_id": 9}, bus, EventInterrupt(), "detect"
        )
    assert session.engine.execute_tool_call.await_count == 1


@pytest.mark.asyncio
async def test_pci_catalog_plan_matches_pin_and_reuses_saved_snapshot(tmp_path):
    session = _create_mock_session(tmp_path)
    preset = ScapPreset(9, "0" * 64)
    source = tmp_path / preset.filename
    raw = (
        Path(__file__).parents[1] / "core/compliance/fixtures/openscap-datastream.xml"
    ).read_bytes()
    raw = raw.replace(
        b"xccdf_example_profile_server", preset.settings()["profile_id"].encode()
    )
    source.write_bytes(raw)
    preset = ScapPreset(9, hashlib.sha256(raw).hexdigest())
    request = _create_mock_request(
        tmp_path, action="assess", baseline_id=None
    ).model_dump()
    request["scap_preset_id"] = preset.id
    request["scan"].update(profile=preset.filename, openscap=preset.settings())

    with (
        patch("kerkov.core.workflows.compliance.scap_preset", return_value=preset),
        patch(
            "kerkov.core.workflows.compliance.ansible_binary",
            return_value="/bin/ansible-playbook",
        ),
        patch(
            "kerkov.core.workflows.compliance.prepare_scap_preset",
            new=AsyncMock(return_value=source),
        ) as prepare,
    ):
        changed = {
            **request["scan"],
            "openscap": {**preset.settings(), "version": "10.*"},
        }
        with pytest.raises(ValueError, match="selected catalog profile"):
            build_run(session, {**request, "scan": changed}, "changed")
        with pytest.raises(ValueError, match="assessment only"):
            build_run(session, {**request, "action": "apply"}, "apply")
        run = build_run(session, request, "scan")
        await run.prepare()
        assert run.record["plan"]["scap_source"] == preset.source()
        assert run.record["label"] == preset.name
        snapshot = Path(run.scan["profile"])
        assert snapshot != source and snapshot.read_bytes() == raw
        await run.flush()
        source.unlink()
        retry = build_run(
            session, {**request, "assessment_of": run.record["id"]}, "retry"
        )
        await retry.prepare()
        assert Path(retry.scan["profile"]).read_bytes() == raw
        prepare.assert_awaited_once()
        await retry.flush()


@pytest.mark.asyncio
async def test_pci_saved_preview_binds_selected_tasks_and_verifies_after_apply(
    tmp_path, monkeypatch
):
    session = _create_mock_session(tmp_path)
    fixture = Path(__file__).parents[1] / "core/compliance/fixtures"
    pci_profile = ScapPreset(9, "0" * 64).settings()["profile_id"]
    source = tmp_path / "ssg-rhel9-ds.xml"
    raw = (
        (fixture / "openscap-datastream.xml")
        .read_bytes()
        .replace(b"xccdf_example_profile_server", pci_profile.encode())
    )
    source.write_bytes(raw)
    preset = ScapPreset(9, hashlib.sha256(raw).hexdigest())
    request = _create_mock_request(
        tmp_path, action="assess", baseline_id=None
    ).model_dump()
    request["scap_preset_id"] = preset.id
    request["scan"].update(profile=preset.filename, openscap=preset.settings())
    rule = "xccdf_org.ssgproject.content_rule_accounts_users_own_home_directories"
    module = "community.general.ini_file"
    module_bytes = b"# fixture module"
    module_sha256 = hashlib.sha256(module_bytes).hexdigest()
    monkeypatch.setitem(
        SCAP_MODULES, module, ScapModule("1.0.0", "a" * 40, module_sha256)
    )
    cache = Path(session.config.compliance_projects_dir) / "scap/remediation-modules"
    cache.mkdir(parents=True)
    (cache / f"{module_sha256}.py").write_bytes(module_bytes)
    signed_tasks = b"""
- name: Pinned PCI fixture
  hosts: all
  vars: {}
  tasks:
    - name: Gather package facts before the rule
      ansible.builtin.package_facts:
        manager: auto
      tags: [always]
    - name: Fix the matching rule
      tags: [accounts_users_own_home_directories]
      block:
        - name: Write approved setting
          community.general.ini_file:
            path: /fixture
            section: review
            option: enabled
            value: "true"
    - name: Leave unrelated rule alone
      ansible.builtin.file:
        path: /unrelated
        state: directory
      tags: [unrelated_rule]
    - name: Gather package facts after the rule
      ansible.builtin.package_facts:
        manager: auto
      tags: [always]
    - name: Gather service facts
      ansible.builtin.service_facts:
      tags: [always]
"""
    source_evidence = tmp_path / "artifacts/compliance/source.xml"
    source_evidence.parent.mkdir(parents=True)
    evidence = (
        (fixture / "openscap-arf.xml")
        .read_bytes()
        .replace(b"xccdf_org.ssgproject.content_profile_standard", pci_profile.encode())
    )
    evidence = evidence.replace(b"<result>pass", b"<result>fail")
    source_evidence.write_bytes(evidence)
    assessment = {
        "engine": "openscap",
        "evidence_status": "scanned",
        "profile_name": pci_profile,
        "profile_sha256": preset.sha256,
        "report_artifact": str(source_evidence),
        "report_sha256": hashlib.sha256(evidence).hexdigest(),
        "target": {"host": "192.0.2.1", "user": "root", "port": 22},
        "sudo": True,
        "control_count": 1,
        "counts": {
            "passed": 0,
            "failed": 1,
            "error": 0,
            "skipped": 0,
            "waived": 0,
            "no_tests": 0,
        },
        "has_inconclusive_results": False,
        "xccdf_counts": {"fail": 1},
    }
    with (
        patch("kerkov.core.workflows.compliance.scap_preset", return_value=preset),
        patch(
            "kerkov.core.workflows.compliance.ansible_binary",
            return_value="/bin/ansible-playbook",
        ),
        patch(
            "kerkov.core.workflows.compliance.prepare_scap_preset",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "kerkov.core.workflows.compliance.prepare_scap_playbook",
            new=AsyncMock(return_value=source),
        ),
        patch(
            "kerkov.core.workflows.compliance.read_scap_playbook",
            return_value=signed_tasks,
        ),
    ):
        scan = build_run(session, request, "scan")
        await scan.prepare()
        scan.record.update(status="attention", before=assessment)
        scan.publish()
        await scan.flush()

        preview_request = {
            **request,
            "action": "preview",
            "scap_fixes_of": scan.record["id"],
            "scap_rule_ids": [rule],
        }
        preview = build_run(session, preview_request, "preview")
        await preview.prepare()
        await preview.finish(EventInterrupt())
        assert preview.record["status"] == "previewed"
        assert preview.assessment_count == 0 and preview.record["remediation"] is None
        assert preview.record["plan"]["scap_fixes"]["selected"] == [rule]
        assert preview.record["plan"]["scap_fixes"]["modules"] == {
            module: {"version": "1.0.0", "sha256": module_sha256}
        }
        playbook = Path(preview.remediation["playbook"]).read_text()
        assert "hosts: selected_asset" in playbook
        assert (
            playbook.index("Gather package facts before the rule")
            < playbook.index("Fix the matching rule")
            < playbook.index("Gather package facts after the rule")
            < playbook.index("Gather service facts")
        )
        assert preview.record["plan"]["scap_fixes"]["task_count"] == 4
        assert "Leave unrelated rule alone" not in playbook
        session.approval_strategy.approve.assert_not_awaited()

        apply_request = {
            **preview_request,
            "action": "apply",
            "preview_id": preview.record["id"],
        }
        with pytest.raises(ValueError, match="preview does not match"):
            await build_run(
                session, {**apply_request, "scap_rule_ids": None}, "different"
            ).prepare()
        apply = build_run(session, apply_request, "apply")
        await apply.prepare()
        assert apply.remediation["check"] is False

        scanner = SimpleNamespace(name="compliance_scan")
        ansible = SimpleNamespace(name="ansible")
        assert not (await apply.approve(ansible, apply.remediation)).approved
        params = {k: v for k, v in apply.scan.items() if v is not None}
        assert (await apply.approve(scanner, params)).approved
        apply.result(
            "compliance_scan",
            ToolResult(
                success=True,
                output=json.dumps(assessment),
                metadata={"status": "completed"},
            ),
        )
        assert (await apply.approve(ansible, apply.remediation)).approved
        apply.result("ansible", ToolResult(success=True, output=""))
        assert (await apply.approve(scanner, params)).approved
        apply.result(
            "compliance_scan",
            ToolResult(
                success=True,
                output=json.dumps(
                    {
                        **assessment,
                        "counts": {**assessment["counts"], "passed": 1, "failed": 0},
                        "xccdf_counts": {"pass": 1},
                    }
                ),
                metadata={"status": "completed"},
            ),
        )
        await apply.finish(EventInterrupt())
        assert apply.record["status"] == "verified"

        tampered = build_run(session, apply_request, "tampered")
        await tampered.prepare()
        tampered_scan = {k: v for k, v in tampered.scan.items() if v is not None}
        assert (await tampered.approve(scanner, tampered_scan)).approved
        tampered.result(
            "compliance_scan",
            ToolResult(
                success=True,
                output=json.dumps(assessment),
                metadata={"status": "completed"},
            ),
        )
        local_module = (
            Path(tampered.remediation["playbook"]).parent
            / "collections/ansible_collections/community/general/plugins/modules/ini_file.py"
        )
        local_module.write_text("# changed module")
        assert not (await tampered.approve(ansible, tampered.remediation)).approved

        stale = build_run(session, apply_request, "stale")
        await stale.prepare()
        source_evidence.write_bytes(evidence.replace(b"<result>fail", b"<result>pass"))
        stale.record["before"] = {
            **assessment,
            "report_sha256": hashlib.sha256(source_evidence.read_bytes()).hexdigest(),
        }
        denied = await stale.approve(ansible, stale.remediation)
        assert not denied.approved
        assert "findings" in denied.user_message
        await stale.finish(EventInterrupt())
        assert stale.record["status"] == "blocked"


@pytest.mark.asyncio
async def test_stop_cancels_pci_catalog_preparation(tmp_path):
    session = _create_mock_session(tmp_path)
    preset = ScapPreset(9, "0" * 64)
    request = _create_mock_request(
        tmp_path, action="assess", baseline_id=None
    ).model_dump()
    request["scap_preset_id"] = preset.id
    request["scan"].update(profile=preset.filename, openscap=preset.settings())
    interrupt = EventInterrupt()

    async def stop_during_download(*args):
        interrupt.interrupt_requested.set()
        await asyncio.Future()

    with (
        patch("kerkov.core.workflows.compliance.scap_preset", return_value=preset),
        patch(
            "kerkov.core.workflows.compliance.ansible_binary",
            return_value="/bin/ansible-playbook",
        ),
        patch(
            "kerkov.core.workflows.compliance.prepare_scap_preset",
            new=stop_during_download,
        ),
    ):
        run = build_run(session, request, "scan")
        with pytest.raises(InterruptedByUser):
            await run.prepare(interrupt)
        await run.finish(interrupt)
    assert run.record["status"] == "interrupted"
    assert run.record["plan"] == {}
    session.approval_strategy.approve.assert_not_awaited()


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
    assert session.mode_manager.current_mode.mode_type is ModeType.PURPLE
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
        assert not any(
            event["type"] == "compliance.event"
            and event["payload"]["event_type"] == "log.warning"
            and "/mode blue" in event["payload"]["payload"].get("message", "")
            for event in events
        )
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
