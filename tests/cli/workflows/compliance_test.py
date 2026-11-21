"""Terminal compliance runs: setup prompts, derived requests and the gated turn."""

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from kerkov.cli.input_manager import InputManager
from kerkov.cli.workflows import compliance
from kerkov.cli.workflows.compliance import (
    SetupCancelled,
    TerminalObserver,
    apply_request,
    collect_request,
    handle,
    reassess_request,
    start_run,
)
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.workflows import WorkflowRequest, new_record
from kerkov.core.knowledge_base.models import LedgerEntry, LedgerSection
from kerkov.core.session.mode_manager import ModeManager
from kerkov.core.session.modes import ModeType
from kerkov.core.workflows.registry import COMPLIANCE_WORKFLOW
from tests.core.workflows.recording import RecordingSink


def _create_mock_rows(with_access=True):
    rows = [
        LedgerEntry(
            id=1,
            engagement_id="root",
            section=LedgerSection.ASSETS,
            entry={"kind": "ip", "value": "192.0.2.1", "name": "web"},
        ),
        LedgerEntry(
            id=2,
            engagement_id="root",
            section=LedgerSection.ASSETS,
            entry={"kind": "url", "value": "https://example.com"},
        ),
    ]
    if with_access:
        rows.append(
            LedgerEntry(
                id=3,
                engagement_id="root",
                section=LedgerSection.CREDENTIALS,
                entry={
                    "kind": "ssh",
                    "host": "192.0.2.1",
                    "username": "audit",
                    "port": 2222,
                    "private_key_file": "/keys/audit",
                    "known_hosts_file": "/keys/known_hosts",
                    "sudo": False,
                },
            )
        )
    return rows


def _create_mock_cli(choices=(), texts=None, rows=None, ledger=True):
    """A CLI stand-in whose pickers answer `choices` in order.

    Text prompts accept their prefilled default unless `texts` maps the label.
    """
    texts = texts or {}
    asked = []

    async def get_text(prompt, default=""):
        label = prompt.removeprefix("<b>").split(":</b>")[0]
        asked.append(label)
        return texts.get(label, default)

    store = (
        SimpleNamespace(
            list=AsyncMock(return_value=_create_mock_rows() if rows is None else rows)
        )
        if ledger
        else None
    )
    manager = ModeManager()
    manager.switch_mode(mode_type=ModeType.PURPLE)
    cli = SimpleNamespace(
        console=Console(record=True, width=160, color_system=None),
        input_manager=SimpleNamespace(
            choose=AsyncMock(side_effect=list(choices)),
            get_text=get_text,
            interrupt_requested=asyncio.Event(),
            reset_interrupt=MagicMock(),
            start_watching_for_interrupt=MagicMock(),
            stop_watching_for_interrupt=MagicMock(),
        ),
        get_resource_ledger=lambda: store,
        mode_manager=manager,
        tool_approval_helper=SimpleNamespace(non_interactive_auto_approve=False),
        require_provider=lambda: True,
        config=SimpleNamespace(execution_enabled=True),
        engagement_store=SimpleNamespace(engagement_id="root"),
        interactive_approval=MagicMock(),
        output_sink=RecordingSink(),
        run_workflow_turn=AsyncMock(),
    )
    cli.asked = asked
    return cli


def _create_mock_request(action="preview", **updates):
    return WorkflowRequest.model_validate(
        {
            "action": action,
            "resource_id": 1,
            "baseline_id": builtin_baselines()[0]["id"],
            "scan": {
                "host": "192.0.2.1",
                "user": "root",
                "profile": builtin_baselines()[0]["profile"],
                "private_key_file": "/keys/root",
                "known_hosts_file": "/keys/known_hosts",
            },
            **updates,
        }
    )


def test_collect_prefills_recorded_ssh_access_for_a_baseline_preview():
    baseline = builtin_baselines()[0]
    cli = _create_mock_cli(choices=["1", baseline["id"], "0", "no", "no"])
    request = asyncio.run(collect_request(cli, "preview"))
    hosts = cli.input_manager.choose.await_args_list[0].args[1]
    # A URL resource cannot be assessed over SSH, so it is not offered.
    assert hosts == [("1", "#1 192.0.2.1 — web (asset)")]
    assert request.action == "preview"
    assert request.baseline_id == baseline["id"]
    assert request.scan.user == "audit" and request.scan.port == 2222
    assert request.scan.private_key_file == "/keys/audit"
    assert request.scan.sudo is False
    assert request.scan.profile == baseline["profile"]
    assert request.remediation is None and request.bootstrap_python is False


def test_collect_custom_profile_assess_skips_remediation_prompts():
    cli = _create_mock_cli(
        choices=["1", compliance.CUSTOM, "yes"],
        texts={
            "SSH user": "root",
            "Private key file": "/k",
            "Trusted known_hosts file": "/kh",
            "Profile project directory": "/profiles/custom",
        },
        rows=_create_mock_rows(with_access=False),
    )
    request = asyncio.run(collect_request(cli, "assess"))
    assert request.baseline_id is None
    assert request.scan.profile == "/profiles/custom"
    assert request.scan.inputs_file is None
    assert request.scan.port == 22
    assert "Saved Ansible profile" not in cli.asked


def test_collect_custom_profile_preview_asks_for_the_ansible_plan():
    cli = _create_mock_cli(
        choices=["1", compliance.CUSTOM, compliance.MANUAL, "yes", "yes"],
        texts={
            "SSH user": "root",
            "Private key file": "/k",
            "Trusted known_hosts file": "/kh",
            "Profile project directory": "/profiles/custom",
            "Saved Ansible profile": "lab",
            "Playbook": "harden",
            "Ansible host alias": "web1",
        },
    )
    request = asyncio.run(collect_request(cli, "preview"))
    assert cli.asked[-4:] == [
        "Saved Ansible profile",
        "Playbook",
        "Ansible host alias",
        "Variables file (optional)",
    ]
    assert request.remediation.profile == "lab"
    assert request.remediation.limit == "web1"
    assert request.remediation.check is True
    assert request.bootstrap_python is True


def test_collect_refuses_inventory_groups_and_needs_a_recorded_host():
    cli = _create_mock_cli(
        choices=["1", compliance.CUSTOM, compliance.MANUAL, "yes", "no"],
        texts={
            "SSH user": "root",
            "Private key file": "/k",
            "Trusted known_hosts file": "/kh",
            "Profile project directory": "/p",
            "Saved Ansible profile": "lab",
            "Playbook": "harden",
            "Ansible host alias": "all",
        },
    )
    with pytest.raises(ValueError, match="single host alias"):
        asyncio.run(collect_request(cli, "preview"))
    with pytest.raises(ValueError, match="/asset add"):
        asyncio.run(collect_request(_create_mock_cli(ledger=False), "assess"))
    with pytest.raises(ValueError, match="No ip or domain asset"):
        asyncio.run(collect_request(_create_mock_cli(rows=[]), "assess"))


def test_escape_at_a_picker_cancels_setup():
    cli = _create_mock_cli(choices=[None])
    with pytest.raises(SetupCancelled):
        asyncio.run(collect_request(cli, "assess"))


def test_apply_reissues_the_preview_with_a_matching_signature():
    preview = _create_mock_request(
        "preview",
        baseline_id=None,
        remediation={"profile": "lab", "playbook": "harden", "limit": "web1"},
    )
    preview.remediation.check = True
    record = new_record(preview, "Custom")
    record["status"] = "previewed"
    request = apply_request(record)
    assert request.action == "apply"
    assert request.preview_id == record["id"]
    assert request.remediation.check is False
    assert request.signature() == record["signature"]
    record["status"] = "attention"
    with pytest.raises(ValueError, match="Only a completed preview"):
        apply_request(record)


def test_reassess_keeps_the_asset_and_scan_settings_but_drops_remediation():
    record = new_record(_create_mock_request("apply", bootstrap_python=True), "B")
    with pytest.raises(ValueError, match="no prepared profile"):
        reassess_request(record)
    record["plan"] = {"profile": "/ws/plan/profile"}
    request = reassess_request(record)
    assert request.action == "assess"
    assert request.assessment_of == record["id"]
    assert request.bootstrap_python is False and request.preview_id is None
    assert request.resource_id == 1
    original = WorkflowRequest.model_validate(record["request"])
    assert request.scan_settings() == original.scan_settings()


def test_observer_narrates_each_stage_once_and_escapes_markup():
    sink = RecordingSink()
    observer = TerminalObserver(sink)
    observer.updated({"stage": "readiness", "detail": "Checking [bold]x[/bold]"})
    observer.updated({"stage": "readiness", "detail": "Still checking"})
    observer.updated({"stage": "assessment", "detail": "Ready"})
    observer.updated({"stage": "finished", "detail": "Done"})
    assert sink.infos == [
        "Compliance readiness: Checking \\[bold]x\\[/bold]",
        "Compliance assessment: Ready",
    ]


def _handle(cli, line, workspace=None):
    with patch.object(compliance, "get_current_workspace", return_value=workspace):
        asyncio.run(handle(cli, COMPLIANCE_WORKFLOW, line.split()))
    return cli.console.export_text()


def test_runs_are_refused_under_one_shot_auto_approval():
    cli = _create_mock_cli()
    cli.tool_approval_helper.non_interactive_auto_approve = True
    with pytest.raises(ValueError, match="interactive CLI"):
        _handle(cli, "assess")
    cli.input_manager.choose.assert_not_awaited()


def test_wrong_arity_prints_usage_and_missing_provider_stops_early():
    with pytest.raises(ValueError, match="apply <run-id>"):
        _handle(_create_mock_cli(), "apply")
    cli = _create_mock_cli()
    cli.require_provider = lambda: False
    _handle(cli, "preview")
    cli.input_manager.choose.assert_not_awaited()


def test_cancelled_setup_starts_nothing(tmp_path):
    cli = _create_mock_cli(choices=[None])
    text = _handle(cli, "assess", tmp_path)
    assert "Compliance setup cancelled." in text
    assert "targets Blue; this engagement is in Purple" in text
    cli.run_workflow_turn.assert_not_awaited()


def test_start_run_sends_the_plan_under_the_overlay_and_records_the_outcome(
    tmp_path,
):
    cli = _create_mock_cli()

    async def scenario():
        with (
            patch.object(compliance, "get_current_workspace", return_value=tmp_path),
            patch.object(compliance.ComplianceRun, "prepare", new=AsyncMock()),
        ):
            await start_run(cli, COMPLIANCE_WORKFLOW, _create_mock_request("assess"))

    asyncio.run(scenario())
    call = cli.run_workflow_turn.await_args
    assert "Assessment arguments:" in call.args[0]
    assert call.kwargs["overlay"] == COMPLIANCE_WORKFLOW.overlay
    approval = call.kwargs["approval"]
    assert call.kwargs["output"] is approval.output
    assert approval.record["status"] == "incomplete"
    cli.input_manager.reset_interrupt.assert_called_once()
    assert "Workflow incomplete" in cli.console.export_text()


def test_stopped_run_is_recorded_as_interrupted(tmp_path):
    cli = _create_mock_cli()

    async def stop(*args, **kwargs):
        cli.input_manager.interrupt_requested.set()

    cli.run_workflow_turn = AsyncMock(side_effect=stop)

    async def scenario():
        with (
            patch.object(compliance, "get_current_workspace", return_value=tmp_path),
            patch.object(compliance.ComplianceRun, "prepare", new=AsyncMock()),
        ):
            await start_run(cli, COMPLIANCE_WORKFLOW, _create_mock_request("assess"))

    asyncio.run(scenario())
    assert "Workflow interrupted" in cli.console.export_text()


@patch("kerkov.cli.input_manager.sys.stdin.isatty", return_value=False)
@patch("kerkov.cli.input_manager.signal.signal")
@patch("kerkov.cli.input_manager.signal.getsignal", return_value=signal.SIG_DFL)
def test_ctrl_c_during_readiness_stops_the_run_not_the_cli(
    mock_getsignal, mock_signal, mock_isatty, tmp_path
):
    cli = _create_mock_cli()
    # A pipe-backed session keeps InputManager from probing a real terminal.
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        input_manager = InputManager(MagicMock())
    cli.input_manager = input_manager
    prepare_cancelled = asyncio.Event()
    watching = []

    async def prepare(run):
        watching.append(input_manager.is_watching_for_interrupt)
        installed = mock_signal.call_args.args[1]
        installed(signal.SIGINT, None)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            prepare_cancelled.set()
            raise

    async def scenario():
        with (
            patch.object(compliance, "get_current_workspace", return_value=tmp_path),
            patch.object(compliance.ComplianceRun, "prepare", new=prepare),
        ):
            await start_run(cli, COMPLIANCE_WORKFLOW, _create_mock_request("assess"))

    asyncio.run(scenario())
    assert watching == [True]
    assert prepare_cancelled.is_set()
    cli.run_workflow_turn.assert_not_awaited()
    assert "Workflow interrupted" in cli.console.export_text()
    assert not input_manager.interrupt_requested.is_set()
    assert not input_manager.is_watching_for_interrupt
    assert mock_signal.call_args.args == (signal.SIGINT, signal.SIG_DFL)


def test_readiness_failure_is_recorded_without_a_turn(tmp_path):
    cli = _create_mock_cli()

    async def scenario():
        with (
            patch.object(compliance, "get_current_workspace", return_value=tmp_path),
            patch.object(
                compliance.ComplianceRun,
                "prepare",
                new=AsyncMock(side_effect=ValueError("Install Docker")),
            ),
        ):
            await start_run(cli, COMPLIANCE_WORKFLOW, _create_mock_request("assess"))

    asyncio.run(scenario())
    cli.run_workflow_turn.assert_not_awaited()
    text = cli.console.export_text()
    assert "Workflow needs review" in text
    assert "✗ Readiness: Install Docker" in text


def test_previewed_run_suggests_the_matching_apply(tmp_path):
    cli = _create_mock_cli()

    async def scenario():
        async def finish(run, interrupt):
            run.record.update(status="previewed", stage="finished")

        with (
            patch.object(compliance, "get_current_workspace", return_value=tmp_path),
            patch.object(compliance.ComplianceRun, "prepare", new=AsyncMock()),
            patch.object(compliance.ComplianceRun, "finish", new=finish),
        ):
            await start_run(cli, COMPLIANCE_WORKFLOW, _create_mock_request())

    asyncio.run(scenario())
    assert "/workflow compliance apply " in cli.console.export_text()
