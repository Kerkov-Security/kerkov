"""Asset/target commands exercised with real engagement persistence."""

import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from kerkov.cli.approval_mode_manager import ApprovalModeManager
from kerkov.cli.engagement_store import EngagementStore
from kerkov.cli.input_parser import InputParser
from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.cli.mode_manager import ModeManager
from kerkov.cli.verbosity_manager import VerbosityManager
from kerkov.core.conversation_engine import ConversationEngine
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)


@pytest.fixture
def make_cli(tmp_path, monkeypatch):
    instances = []
    workspace_token = set_current_workspace(None)

    def create():
        cli = KerkovCLI.__new__(KerkovCLI)
        cli.console = Console(file=StringIO(), width=120, color_system=None)
        monkeypatch.setattr("kerkov.cli.kerkov_cli.console", cli.console)
        cli.config = SimpleNamespace(kb_enabled=True, kb_embeddings=False)
        cli.engagement_store = EngagementStore(tmp_path / "engagements")
        cli.workspace_manager = RunWorkspaceManager(tmp_path / "workspaces")
        cli.provder_manager = SimpleNamespace(provider=None)
        cli.mode_manager = ModeManager()
        cli.verbosity_manager = VerbosityManager()
        cli.approval_mode_manager = ApprovalModeManager()
        cli.skill_manager = SimpleNamespace(get_catalog_summary=lambda: "")
        cli.agent_manager = SimpleNamespace(get_index_summary=lambda: "")
        cli.tool_approval_helper = MagicMock()
        cli.token_tracker = MagicMock()
        cli.conversation_history = []
        cli._knowledge_base = None
        cli._findings_ledger = None
        instances.append(cli)
        return cli

    yield create
    for cli in instances:
        if cli._knowledge_base is not None:
            cli._knowledge_base.close()
    reset_current_workspace(workspace_token)


def execute(cli, command):
    asyncio.run(InputParser.parse_for_message_type(command).execute(cli))


def output(cli):
    return cli.console.file.getvalue()


def entries(cli, section):
    return asyncio.run(cli.get_resource_ledger().list(section))


def test_list_and_remove_before_chat_do_not_create_engagement(make_cli):
    cli = make_cli()
    for command in (
        "/asset",
        "/assets list",
        "/target",
        "/targets",
        "/target remove 1",
    ):
        execute(cli, command)
    assert cli.engagement_store.engagement_id is None
    assert not list(cli.engagement_store.store_dir.iterdir())
    assert "No assets" in output(cli)
    assert "No targets" in output(cli)


@pytest.mark.parametrize(
    "command",
    [
        "/asset add",
        "/target add ip",
        "/asset add domain bad/name",
        "/target add ip 999.1.2.3",
        "/asset add url not-a-url",
        '/asset add repository "relative/repo"',
        '/asset add domain "unterminated',
        "/asset remove 0",
        "/target remove nope",
        "/asset list extra",
        "/target add ip 10.0.0.5 extra arguments",
        "/target unknown",
    ],
)
def test_invalid_commands_do_not_create_engagement(make_cli, command):
    cli = make_cli()
    execute(cli, command)
    assert cli.engagement_store.engagement_id is None
    assert output(cli)
    assert "Saved" not in output(cli)


def test_add_list_and_section_scoped_remove_without_provider(make_cli):
    cli = make_cli()
    execute(cli, '/asset add url https://API.example.com/Path "Our [API]"')
    execute(cli, "/target add url https://api.example.com/Path")
    assets = entries(cli, LedgerSection.ASSETS)
    targets = entries(cli, LedgerSection.TARGETS)
    assert assets[0].entry["name"] == "Our [API]"
    assert assets[0].id != targets[0].id
    assert len(cli.conversation_history) == 1
    assert cli.provder_manager.provider is None
    execute(cli, "/assets")
    execute(cli, "/targets list")
    assert "Our [API]" in output(cli)
    execute(cli, f"/asset remove {targets[0].id}")
    assert len(entries(cli, LedgerSection.TARGETS)) == 1
    execute(cli, f"/asset remove {assets[0].id}")
    assert entries(cli, LedgerSection.ASSETS) == []
    execute(cli, f"/target remove {targets[0].id}")
    assert entries(cli, LedgerSection.TARGETS) == []


def test_quoted_repository_and_name_are_preserved(make_cli, tmp_path):
    cli = make_cli()
    repository = tmp_path / "Source Code"
    execute(cli, f'/asset add repository "{repository}" "Application source"')
    record = entries(cli, LedgerSection.ASSETS)[0]
    assert record.entry["value"] == str(repository)
    assert record.entry["name"] == "Application source"


def test_resume_restores_inventory_and_clear_starts_separate_inventory(make_cli):
    cli = make_cli()
    execute(cli, "/asset add domain EXAMPLE.com")
    original_id = cli.engagement_store.engagement_id
    restored = make_cli()
    assert restored.restore_engagement(original_id) is not None
    execute(restored, "/asset")
    assert "example.com" in output(restored)
    restored.clear_history()
    assert restored.get_resource_ledger() is None
    execute(restored, "/target add ip 10.0.0.5")
    assert restored.engagement_store.engagement_id != original_id
    assert entries(restored, LedgerSection.ASSETS) == []
    assert len(entries(cli, LedgerSection.ASSETS)) == 1


def test_resources_are_available_to_model_outside_conversation_history(make_cli):
    cli = make_cli()
    execute(cli, "/asset add domain protected.example")
    execute(cli, "/target add ip 10.0.0.5")
    session = SimpleNamespace(
        ledger=cli.get_resource_ledger(),
        ledger_block=None,
        provider=SimpleNamespace(caches_prompt_prefix=False),
        history=cli.conversation_history,
        transient_prompts=[],
    )
    engine = ConversationEngine()
    asyncio.run(engine._refresh_ledger_block(session))
    messages = engine._messages_for_provider(session)
    assert "protected.example" in messages[1].content
    assert "10.0.0.5" in messages[1].content
    assert len(cli.conversation_history) == 1


def test_disabled_ledger_explains_requirement_without_creating_engagement(make_cli):
    cli = make_cli()
    cli.config.kb_enabled = False
    execute(cli, "/asset add ip 10.0.0.5")
    assert "KB_ENABLED=true" in output(cli)
    assert cli.engagement_store.engagement_id is None


def test_unavailable_ledger_does_not_report_success(make_cli, monkeypatch):
    cli = make_cli()
    monkeypatch.setattr(cli, "_open_knowledge_base", lambda engagement_id: None)
    execute(cli, "/target add ip 10.0.0.5")
    assert "ledger is unavailable" in output(cli)
    assert "Saved" not in output(cli)
