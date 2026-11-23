"""`/workflow`: listing, dispatch and reading saved compliance runs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from kerkov.cli.input_parser import InputParser
from kerkov.cli.messages.workflow_message import HANDLERS, WorkflowMessage
from kerkov.core.compliance.workflows import WorkflowRequest, WorkflowStore, new_record
from kerkov.core.session.mode_manager import ModeManager
from kerkov.core.session.modes import ModeType
from kerkov.core.workflows.registry import workflow_names


def _create_mock_cli(mode=ModeType.PURPLE):
    manager = ModeManager()
    manager.switch_mode(mode_type=mode)
    return SimpleNamespace(
        console=Console(record=True, width=160, color_system=None),
        mode_manager=manager,
    )


def _create_mock_record(workspace, status="previewed", run_id=None):
    request = WorkflowRequest.model_validate(
        {
            "action": "preview",
            "resource_id": 1,
            "scan": {
                "host": "192.0.2.1",
                "user": "root",
                "profile": "/profiles/fixture",
                "private_key_file": "/private/key",
                "known_hosts_file": "/private/trust",
            },
        }
    )
    record = new_record(request, "Fixture baseline")
    if run_id:
        record["id"] = run_id
    record.update(status=status, stage="finished", detail="Preview completed.")
    WorkflowStore(workspace).save(record)
    return record


def _run(cli, line, workspace=None):
    message = InputParser.parse_for_message_type(line)
    assert isinstance(message, WorkflowMessage)
    with patch(
        "kerkov.cli.workflows.compliance.get_current_workspace",
        return_value=workspace,
    ):
        asyncio.run(message.execute(cli))
    return cli.console.export_text()


def test_every_registered_workflow_has_a_terminal_handler():
    assert set(HANDLERS) == set(workflow_names())


def test_list_names_each_workflow_and_flags_an_inactive_role():
    text = _run(_create_mock_cli(ModeType.PURPLE), "/workflow")
    assert "compliance" in text and "Blue (not active)" in text
    text = _run(_create_mock_cli(ModeType.BLUE), "/workflows list")
    assert "Blue" in text and "not active" not in text


def test_unknown_workflow_names_the_available_ones():
    text = _run(_create_mock_cli(), "/workflow recon runs")
    assert "Unknown workflow 'recon'. Available: compliance." in text


def test_runs_need_an_engagement_workspace():
    text = _run(_create_mock_cli(), "/workflow compliance runs")
    assert "Start or resume an engagement first" in text


def test_runs_lists_saved_records_and_flags_abandoned_ones(tmp_path):
    previewed = _create_mock_record(tmp_path)
    abandoned = _create_mock_record(tmp_path, status="running")
    text = _run(_create_mock_cli(), "/workflow compliance runs", tmp_path)
    assert previewed["id"][:12] in text and abandoned["id"][:12] in text
    assert "previewed" in text
    # No terminal run is live, so a record still marked running was abandoned.
    assert "interrupted" in text and "running" not in text


def test_runs_with_no_records(tmp_path):
    text = _run(_create_mock_cli(), "/workflow compliance", tmp_path)
    assert "No compliance runs in this engagement." in text


def test_show_accepts_a_unique_prefix_and_renders_the_run(tmp_path):
    record = _create_mock_record(tmp_path, run_id="abcdef" + "0" * 26)
    text = _run(_create_mock_cli(), "/workflow compliance show ABCDEF", tmp_path)
    assert "Preview ready for review · 192.0.2.1 · Fixture baseline" in text
    assert f"Run {record['id']}" in text


def test_show_rejects_short_ambiguous_and_unknown_prefixes(tmp_path):
    _create_mock_record(tmp_path, run_id="abcdef1" + "0" * 25)
    _create_mock_record(tmp_path, run_id="abcdef2" + "0" * 25)
    cli = _create_mock_cli()
    assert "at least 6 characters" in _run(
        cli, "/workflow compliance show abc", tmp_path
    )
    assert "matches several runs" in _run(
        cli, "/workflow compliance show abcdef", tmp_path
    )
    assert "No recent compliance run starts with 123456" in _run(
        cli, "/workflow compliance show 123456", tmp_path
    )


def test_unknown_action_prints_usage(tmp_path):
    text = _run(_create_mock_cli(), "/workflow compliance frobnicate", tmp_path)
    assert "Use /workflow compliance runs" in text
