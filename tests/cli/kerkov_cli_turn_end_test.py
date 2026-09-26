"""Tests for the end-of-turn affordance that surfaces silent/backgrounded turns."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from kerkov.cli.kerkov_cli import KerkovCLI
from kerkov.models.basic_models import LLMResponse, ToolCall


def _create_mock_cli(active_sessions) -> KerkovCLI:
    cli = KerkovCLI.__new__(KerkovCLI)
    cli.output_sink = MagicMock()
    cli.agent_supervisor = SimpleNamespace(active=lambda: active_sessions)
    return cli


def _response(content: str = "", tool_calls=None) -> LLMResponse:
    return LLMResponse(
        content=content, model="m", usage=None, tool_calls=tool_calls
    )


def test_empty_turn_with_no_agents_is_flagged() -> None:
    cli = _create_mock_cli([])

    cli._announce_turn_end(_response(content="   "))

    cli.output_sink.info.assert_called_once()
    assert "no text" in cli.output_sink.info.call_args.args[0]


def test_turn_with_visible_text_says_nothing() -> None:
    cli = _create_mock_cli([])

    cli._announce_turn_end(_response(content="Here are the findings."))

    cli.output_sink.info.assert_not_called()


def test_a_tool_only_turn_is_not_treated_as_silent() -> None:
    cli = _create_mock_cli([])

    cli._announce_turn_end(
        _response(tool_calls=[ToolCall(tool_name="exec", parameters={})])
    )

    cli.output_sink.info.assert_not_called()


def test_running_agents_are_announced() -> None:
    cli = _create_mock_cli([MagicMock(), MagicMock()])

    cli._announce_turn_end(_response(content="Dispatched two reviews."))

    cli.output_sink.info.assert_called_once()
    assert "2 agents still running" in cli.output_sink.info.call_args.args[0]


def test_one_running_agent_is_singular() -> None:
    cli = _create_mock_cli([MagicMock()])

    cli._announce_turn_end(_response(content="Dispatched a review."))

    assert "1 agent still running" in cli.output_sink.info.call_args.args[0]


def test_none_response_says_nothing() -> None:
    cli = _create_mock_cli([])

    cli._announce_turn_end(None)

    cli.output_sink.info.assert_not_called()
