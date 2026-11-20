"""How the CLI runs a workflow's plan as a turn."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.kerkov_cli import KerkovCLI


def _create_mock_cli() -> KerkovCLI:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.conversation_history = []
    cli._deliver_agent_reports = MagicMock()
    cli._append_message_to_conversation = cli.conversation_history.append
    cli._run_turn = AsyncMock()
    return cli


def test_workflow_plan_is_context_only_and_the_overlay_is_transient() -> None:
    cli = _create_mock_cli()
    approval, output = MagicMock(), MagicMock()

    asyncio.run(
        cli.run_workflow_turn(
            "Compliance plan action: assess.",
            approval=approval,
            output=output,
            overlay="WORKFLOW: COMPLIANCE",
        )
    )

    cli._deliver_agent_reports.assert_called_once()
    [message] = cli.conversation_history
    assert message.role == "user" and message.context_only is True
    assert message.content == "Compliance plan action: assess."
    cli._run_turn.assert_awaited_once_with(
        approval=approval,
        output=output,
        transient_prompts=["WORKFLOW: COMPLIANCE"],
        interrupt_ends_turn=True,
        context_only=True,
    )


def test_turn_appends_workflow_overlays_after_session_prompts() -> None:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli._ensure_compactor = AsyncMock()
    cli._transient_system_prompts = ["NON-INTERACTIVE"]
    cli._non_interactive = False
    cli._compaction_warning_shown = False
    cli.conversation_history = []
    cli.tools = []
    cli._knowledge_base = None
    cli._findings_ledger = None
    cli.compactor = None
    cli.input_manager = MagicMock()
    cli.kernel = MagicMock()
    cli.kernel.build_loop_session.return_value = SimpleNamespace(
        compaction_warning_shown=False
    )
    cli.engine = SimpleNamespace(run_loop=AsyncMock(return_value=None))
    cli._start_switcher_watch = MagicMock()
    cli._stop_switcher_watch = AsyncMock()
    cli._announce_turn_end = MagicMock()

    asyncio.run(
        cli._run_turn(
            approval=MagicMock(),
            output=MagicMock(),
            transient_prompts=["WORKFLOW"],
            interrupt_ends_turn=True,
            context_only=True,
        )
    )

    kwargs = cli.kernel.build_loop_session.call_args.kwargs
    assert kwargs["transient_prompts"] == ["NON-INTERACTIVE", "WORKFLOW"]
    assert kwargs["interrupt_ends_turn"] is True
    assert kwargs["context_only"] is True
    assert cli._transient_system_prompts == ["NON-INTERACTIVE"]
