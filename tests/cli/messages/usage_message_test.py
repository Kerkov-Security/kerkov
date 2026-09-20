"""Usage command reads saved measurements without making model requests."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kerkov.cli.engagement_store import EngagementStore
from kerkov.cli.messages.usage_message import UsageMessage
from kerkov.models.basic_models import TokenUsage

MODULE = "kerkov.cli.messages.usage_message"


@pytest.fixture
def cli(tmp_path):
    return SimpleNamespace(
        console=MagicMock(),
        config=SimpleNamespace(model_pricing_file=None),
        engagement_store=SimpleNamespace(engagement_id="current", store_dir=tmp_path),
    )


def _output(cli):
    return "\n".join(call.args[0] for call in cli.console.print.call_args_list)


def test_usage_defaults_to_active_engagement_and_renders_plain_text(cli):
    with (
        patch(f"{MODULE}.summarize_engagement", return_value={"total": 42}) as read,
        patch(f"{MODULE}.format_summary", return_value="Model [bold]literal[/bold]"),
    ):
        asyncio.run(UsageMessage().execute(cli))

    read.assert_called_once_with(cli.engagement_store.store_dir, "current", None)
    cli.console.print.assert_called_once_with(
        "Model [bold]literal[/bold]", markup=False, highlight=False
    )


def test_usage_can_read_saved_engagement_without_an_active_one(cli):
    cli.engagement_store.engagement_id = None
    with (
        patch(f"{MODULE}.summarize_engagement", return_value={}) as read,
        patch(f"{MODULE}.format_summary", return_value="Saved usage"),
    ):
        asyncio.run(UsageMessage("saved").execute(cli))

    read.assert_called_once_with(cli.engagement_store.store_dir, "saved", None)
    assert "Saved usage" in _output(cli)


def test_usage_loads_explicit_price_file(cli):
    cli.config.model_pricing_file = "~/model-prices.json"
    prices = {"provider/model": {"input": 1, "output": 2}}
    with (
        patch(f"{MODULE}.load_prices", return_value=prices) as load,
        patch(f"{MODULE}.summarize_engagement", return_value={}) as read,
        patch(f"{MODULE}.format_summary", return_value="Estimated cost"),
    ):
        asyncio.run(UsageMessage().execute(cli))

    load.assert_called_once_with(Path("~/model-prices.json").expanduser())
    read.assert_called_once_with(cli.engagement_store.store_dir, "current", prices)


@pytest.mark.parametrize("error", [ValueError("invalid rates"), OSError("unreadable")])
def test_invalid_prices_still_show_usage_with_unknown_costs(cli, error):
    cli.config.model_pricing_file = "bad-prices.json"
    with (
        patch(f"{MODULE}.load_prices", side_effect=error),
        patch(f"{MODULE}.summarize_engagement", return_value={}) as read,
        patch(f"{MODULE}.format_summary", return_value="Known tokens: 123"),
    ):
        asyncio.run(UsageMessage().execute(cli))

    read.assert_called_once_with(cli.engagement_store.store_dir, "current", None)
    assert "unknown costs" in _output(cli)
    assert "Known tokens: 123" in _output(cli)


def test_usage_without_engagement_explains_how_to_select_one(cli):
    cli.engagement_store.engagement_id = None
    with patch(f"{MODULE}.summarize_engagement") as read:
        asyncio.run(UsageMessage().execute(cli))

    read.assert_not_called()
    assert "No active engagement" in _output(cli)
    assert "/usage <id>" in _output(cli)


@pytest.mark.parametrize("error", [ValueError("unknown ID"), OSError("unreadable")])
def test_usage_read_errors_are_friendly(cli, error):
    with patch(f"{MODULE}.summarize_engagement", side_effect=error):
        asyncio.run(UsageMessage().execute(cli))

    assert "Could not read usage" in _output(cli)
    assert "/engagements" in _output(cli)
    assert cli.console.print.call_args.kwargs["markup"] is False


def test_invalid_usage_does_not_read_saved_data(cli):
    with patch(f"{MODULE}.summarize_engagement") as read:
        asyncio.run(UsageMessage(invalid=True).execute(cli))

    read.assert_not_called()
    assert _output(cli) == "Usage: /usage [engagement-id]"


def test_usage_renders_main_and_agent_measurements_with_explicit_prices(cli, tmp_path):
    main = EngagementStore(store_dir=tmp_path)
    root_id = main.start_engagement("audit", "main", "standard", "normal")
    main.record_model_call(
        provider="TestProvider",
        model="main",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        elapsed_seconds=1,
        status="completed",
    )
    agent = EngagementStore(store_dir=tmp_path)
    child_id = agent.start_engagement(
        "agent",
        "small",
        "silent",
        "auto",
        kind="agent",
        parent_engagement_id=root_id,
        routing_tier="small",
    )
    agent.record_model_call(
        provider="TestProvider",
        model="small",
        usage=TokenUsage(prompt_tokens=50, completion_tokens=5, total_tokens=55),
        elapsed_seconds=0.5,
        status="completed",
    )
    agent.record_run_outcome(
        status="completed",
        elapsed_seconds=0.5,
        iterations=1,
        report_status="completed",
    )
    prices_path = tmp_path / "prices.json"
    prices_path.write_text(
        json.dumps(
            {
                "version": 1,
                "models": {
                    "TestProvider/main": {"input": 1, "output": 2},
                    "TestProvider/small": {"input": 1, "output": 2},
                },
            }
        ),
        encoding="utf-8",
    )
    cli.engagement_store = main
    cli.config.model_pricing_file = str(prices_path)

    asyncio.run(UsageMessage().execute(cli))

    output = _output(cli)
    assert "150 input / 15 output" in output
    assert "Estimated cost (USD): $0.000180" in output
    assert "TestProvider/small [small]" in output
    assert root_id in output
    assert child_id in output
