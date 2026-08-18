"""Tests for how the CLI gates compaction on a live findings ledger."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.context.token_tracker import TokenTracker


def _create_mock_cli(ledger) -> KerkovCLI:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.provder_manager = SimpleNamespace(
        provider=SimpleNamespace(get_max_context_tokens=AsyncMock(return_value=50000))
    )
    cli.config = SimpleNamespace(
        max_context_tokens=50000,
        compaction_threshold_pct=0.65,
        summary_reserve_tokens=2000,
        kb_enabled=True,
    )
    cli.token_tracker = TokenTracker()
    cli.compactor = None
    cli._compaction_warning_shown = False
    cli._findings_ledger = ledger
    return cli


def test_compaction_is_relaxed_only_when_a_ledger_exists() -> None:
    cli = _create_mock_cli(MagicMock())

    asyncio.run(cli._ensure_compactor())

    assert cli.compactor is not None
    assert cli.compactor.ledger_active is True


def test_failed_knowledge_base_keeps_compaction_exhaustive() -> None:
    """kb_enabled is true but the store failed to open — facts have no other home."""
    cli = _create_mock_cli(None)

    asyncio.run(cli._ensure_compactor())

    assert cli.compactor is not None
    assert cli.compactor.ledger_active is False


def test_reused_compactor_picks_up_the_ledger() -> None:
    cli = _create_mock_cli(None)
    asyncio.run(cli._ensure_compactor())
    first = cli.compactor

    cli._findings_ledger = MagicMock()
    asyncio.run(cli._ensure_compactor())

    assert cli.compactor is first
    assert cli.compactor.ledger_active is True
