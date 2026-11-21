"""CliElicitor: how an MCP server's question is put to the CLI operator."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from rich.console import Console

from kerkov.cli.mcp_elicitation import CliElicitor
from kerkov.core.mcp.elicitation import ElicitationRequest, parse_form_schema

_SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "description": "Target host"},
        "port": {"type": "integer", "minimum": 1, "default": 443},
        "api_token": {"type": "string"},
    },
    "required": ["host"],
}


def _create_mock_cli(text_answers, secret_answers=()):
    input_manager = MagicMock(is_watching_for_interrupt=False)
    input_manager.get_text = AsyncMock(side_effect=list(text_answers))
    input_manager.get_secret = AsyncMock(side_effect=list(secret_answers))
    return SimpleNamespace(
        console=Console(record=True, width=120), input_manager=input_manager
    )


def _form() -> ElicitationRequest:
    return ElicitationRequest(
        server="intel",
        mode="form",
        message="Which host should I scan?",
        fields=tuple(parse_form_schema(_SCHEMA)),
        schema=_SCHEMA,
    )


def test_a_form_is_answered_field_by_field_with_defaults() -> None:
    cli = _create_mock_cli(["y", "10.0.0.5", ""], ["hunter2"])

    response = asyncio.run(CliElicitor(cli).elicit(_form()))

    assert response.action == "accept"
    assert response.content == {
        "host": "10.0.0.5",
        "port": 443,
        "api_token": "hunter2",
    }
    output = cli.console.export_text()
    assert "MCP server intel asks: Which host should I scan?" in output
    assert "Do not give it passwords or keys" in output
    assert "hunter2" not in output


def test_a_bad_answer_is_explained_and_asked_again() -> None:
    cli = _create_mock_cli(["y", "10.0.0.5", "eighty", "0", "8080"], [""])

    response = asyncio.run(CliElicitor(cli).elicit(_form()))

    assert response.content == {"host": "10.0.0.5", "port": 8080}
    output = cli.console.export_text()
    assert "enter a whole number" in output
    assert "port: fails 'minimum'" in output


def test_declining_and_cancelling_are_different_answers() -> None:
    declined = asyncio.run(CliElicitor(_create_mock_cli(["n"])).elicit(_form()))
    cancelled = asyncio.run(
        CliElicitor(_create_mock_cli([KeyboardInterrupt()])).elicit(_form())
    )
    midway = asyncio.run(
        CliElicitor(_create_mock_cli(["y", EOFError()])).elicit(_form())
    )

    assert declined.action == "decline"
    assert cancelled.action == "cancel"
    assert midway.action == "cancel"


def test_a_page_opens_only_with_consent() -> None:
    request = ElicitationRequest(
        server="intel",
        mode="url",
        message="Approve the connection.",
        url="https://user:pw@portal.example.com/approve?id=1",
    )
    opened = []
    yes = _create_mock_cli(["y"])
    no = _create_mock_cli([""])

    accepted = asyncio.run(CliElicitor(yes, open_browser=opened.append).elicit(request))
    refused = asyncio.run(CliElicitor(no, open_browser=opened.append).elicit(request))

    assert accepted.action == "accept" and refused.action == "decline"
    assert opened == ["https://user:pw@portal.example.com/approve?id=1"]
    assert "Page: https://portal.example.com" in yes.console.export_text()
