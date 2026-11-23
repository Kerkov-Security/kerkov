"""Elicitation: form parsing, answer checks and the round trip to a real server."""

import asyncio
import json
from pathlib import Path
from typing import Any, List, Optional

import pytest

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.core.mcp.elicitation import (
    ElicitationRequest,
    ElicitationResponse,
    Elicitor,
    answered_by,
    check_content,
    coerce_answer,
    elicitation_callback,
    parse_form_schema,
    unanswerable,
)
from kerkov.core.mcp.manager import McpManager

_FORM = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "title": "Host", "minLength": 3},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 443},
        "mode": {
            "type": "string",
            "oneOf": [
                {"const": "safe", "title": "Safe checks"},
                {"const": "full", "title": "Full scan"},
            ],
        },
        "checks": {"type": "array", "items": {"enum": ["tls", "headers", "cors"]}},
        "confirm": {"type": "boolean"},
    },
    "required": ["host", "confirm"],
}


def test_parses_every_field_kind_the_spec_allows() -> None:
    fields = {f.name: f for f in parse_form_schema(_FORM)}

    assert fields["host"].kind == "string" and fields["host"].required
    assert fields["host"].label == "Host"
    assert fields["port"].kind == "integer" and fields["port"].default == 443
    assert fields["mode"].kind == "choice"
    assert fields["mode"].choices == ("safe", "full")
    assert fields["mode"].choice_labels == ("Safe checks", "Full scan")
    assert fields["checks"].kind == "choices"
    assert fields["checks"].choices == ("tls", "headers", "cors")
    assert fields["confirm"].kind == "boolean" and fields["confirm"].required


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"nested": {"type": "object"}}},
        {"type": "object", "properties": {"list": {"type": "array"}}},
        {"type": "array"},
        {"type": "object"},
    ],
)
def test_a_form_outside_the_spec_subset_is_refused(schema) -> None:
    assert parse_form_schema(schema) is None


@pytest.mark.parametrize(
    "name, text, value, problem",
    [
        ("port", "8080", 8080, None),
        ("port", "eighty", None, "enter a whole number"),
        ("confirm", "yes", True, None),
        ("confirm", "maybe", None, "answer yes or no"),
        ("confirm", "", None, "confirm is required"),
        ("mode", "2", "full", None),
        ("mode", "Safe checks", "safe", None),
        ("mode", "fast", None, "pick one of the listed options"),
        ("checks", "1, cors", ["tls", "cors"], None),
        ("host", "", None, "Host is required"),
    ],
)
def test_typed_answers_are_coerced_or_explained(name, text, value, problem) -> None:
    fields = {f.name: f for f in parse_form_schema(_FORM)}

    assert coerce_answer(fields[name], text) == (value, problem)


def test_content_checks_name_the_field_never_the_value() -> None:
    problem = check_content(_FORM, {"host": "ab", "confirm": True})

    assert problem == "host: fails 'minLength'"
    assert "ab" not in problem
    assert check_content(_FORM, {"host": "abc", "confirm": False}) is None


class _Recorder(Elicitor):
    def __init__(self, response: ElicitationResponse) -> None:
        self.response = response
        self.requests: List[ElicitationRequest] = []

    async def elicit(self, request: ElicitationRequest) -> ElicitationResponse:
        self.requests.append(request)
        return self.response


def _manager(tmp_path: Path):
    pytest.importorskip("mcp")
    from mcp import Client
    from mcp.server.mcpserver import Context, MCPServer
    from mcp_types import (
        ElicitRequest,
        ElicitRequestFormParams,
        ElicitRequestURLParams,
        InputRequiredResult,
    )

    server = MCPServer("demo")

    @server.tool()
    async def scan(ctx: Context) -> Any:
        """Asks which host to scan."""
        if ctx.input_responses:
            answer = ctx.input_responses["scope"]
            return json.dumps({"action": answer.action, "content": answer.content})
        form = ElicitRequestFormParams(
            message="Which host?",
            requestedSchema={
                "type": "object",
                "properties": {"host": {"type": "string"}},
                "required": ["host"],
            },
        )
        return InputRequiredResult(inputRequests={"scope": ElicitRequest(params=form)})

    @server.tool()
    async def connect(ctx: Context) -> Any:
        """Sends the operator to a page first."""
        if ctx.input_responses:
            return ctx.input_responses["page"].action
        page = ElicitRequestURLParams(
            message="Approve the connection.",
            url="https://portal.example.com/approve?id=1",
            elicitationId="e1",
        )
        return InputRequiredResult(inputRequests={"page": ElicitRequest(params=page)})

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"demo": {"command": "x"}}}))
    return McpManager(
        path,
        credentials=CredentialManager(
            environment={}, persistent_store=MemorySecretStore()
        ),
        client_factory_provider=lambda resolved: lambda: Client(
            server, elicitation_callback=elicitation_callback(resolved.config.key)
        ),
        environment={},
    )


def _call(tmp_path: Path, tool: str, elicitor: Optional[Elicitor], agent=False):
    manager = _manager(tmp_path)

    async def call():
        if agent:
            unanswerable()
        tools = {t.original_name: t for t in manager.tools()}
        return await tools[tool].execute({})

    async def scenario():
        await manager.start(set())
        try:
            with answered_by(elicitor):
                # A separate task, as the supervisor runs an agent.
                return await asyncio.create_task(call())
        finally:
            await manager.close()

    return asyncio.run(scenario())


def test_an_answered_form_completes_the_call(tmp_path: Path) -> None:
    recorder = _Recorder(ElicitationResponse("accept", {"host": "10.0.0.5"}))

    result = _call(tmp_path, "scan", recorder)

    assert result.success
    assert json.loads(result.output) == {
        "action": "accept",
        "content": {"host": "10.0.0.5"},
    }
    (request,) = recorder.requests
    assert (request.server, request.mode, request.message) == (
        "demo",
        "form",
        "Which host?",
    )
    assert [f.name for f in request.fields] == ["host"]


def test_a_declined_form_tells_the_server(tmp_path: Path) -> None:
    result = _call(tmp_path, "scan", _Recorder(ElicitationResponse("decline")))

    assert json.loads(result.output) == {"action": "decline", "content": None}


def test_a_page_request_shows_its_origin_and_returns_consent(tmp_path: Path) -> None:
    recorder = _Recorder(ElicitationResponse("accept"))

    result = _call(tmp_path, "connect", recorder)

    assert result.output == "accept"
    (request,) = recorder.requests
    assert request.mode == "url"
    assert request.url_origin == "https://portal.example.com"


@pytest.mark.parametrize("agent", [False, True])
def test_with_nobody_to_ask_the_call_fails_with_a_reason(tmp_path, agent) -> None:
    recorder = _Recorder(ElicitationResponse("accept", {"host": "x"}))

    result = _call(tmp_path, "scan", None if not agent else recorder, agent=agent)

    assert not result.success
    assert "nobody can answer it here" in result.error
    assert recorder.requests == []
