"""Put an MCP server's mid-call request for input in front of the CLI operator."""

from __future__ import annotations

import re
import webbrowser
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from rich.markup import escape

from kerkov.cli.messages.mcp_message import ask_operator
from kerkov.core.mcp.elicitation import (
    ElicitationField,
    ElicitationRequest,
    ElicitationResponse,
    Elicitor,
    check_content,
    coerce_answer,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

_SECRET_NAME = re.compile(r"(?i)token|secret|passw|api[-_]?key|credential|pin\b")


class CliElicitor(Elicitor):
    """Asks one field at a time; Ctrl-C or end of input cancels the whole request."""

    def __init__(
        self,
        cli: "KerkovCLI",
        *,
        open_browser: Callable[[str], Any] = webbrowser.open,
    ) -> None:
        self._cli = cli
        self._open_browser = open_browser

    async def elicit(self, request: ElicitationRequest) -> ElicitationResponse:
        # Agent approvals and the agent picker read stdin too; a private lock
        # would let this question and its prompts land under them.
        async with self._cli.input_manager.takeover():
            console = self._cli.console
            console.print()
            console.print(
                f"[bold]MCP server {escape(request.server)} asks:[/bold] "
                f"{escape(request.message)}"
            )
            if request.mode == "url":
                return await self._url(request)
            return await self._form(request)

    async def _url(self, request: ElicitationRequest) -> ElicitationResponse:
        console = self._cli.console
        console.print(f"  Page: [bold]{escape(request.url_origin)}[/bold]")
        console.print(f"  [dim]{escape(request.url or '')}[/dim]")
        answer = await ask_operator(self._cli, "Open it in the browser? [y/N] ")
        if answer is None:
            return ElicitationResponse(action="cancel")
        if answer.strip().lower() not in ("y", "yes"):
            return ElicitationResponse(action="decline")
        self._open_browser(request.url or "")
        return ElicitationResponse(action="accept")

    async def _form(self, request: ElicitationRequest) -> ElicitationResponse:
        console = self._cli.console
        console.print(
            "  [dim]What you enter goes to that server only. Do not give it "
            "passwords or keys.[/dim]"
        )
        answer = await ask_operator(self._cli, "Answer it? [Y/n] ")
        if answer is None:
            return ElicitationResponse(action="cancel")
        if answer.strip().lower() not in ("", "y", "yes"):
            return ElicitationResponse(action="decline")
        content: Dict[str, Any] = {}
        for item in request.fields:
            value = await self._field(request, item)
            if value is _CANCELLED:
                return ElicitationResponse(action="cancel")
            if value is not None:
                content[item.name] = value
        return ElicitationResponse(action="accept", content=content)

    async def _field(self, request: ElicitationRequest, item: ElicitationField) -> Any:
        console = self._cli.console
        if item.description:
            console.print(f"  [dim]{escape(item.description)}[/dim]")
        for number, label in enumerate(item.choice_labels, start=1):
            console.print(f"    {number}. {escape(label)}")
        prompt = f"{item.label}{_hint(item)}: "
        secret = bool(_SECRET_NAME.search(item.name))
        single = {"type": "object", "properties": {item.name: _property(request, item)}}
        while True:
            text = await ask_operator(self._cli, prompt, secret=secret)
            if text is None:
                return _CANCELLED
            if not text.strip() and item.default is not None:
                return item.default
            value, problem = coerce_answer(item, text)
            if problem is None and value is not None:
                problem = check_content(single, {item.name: value})
            if problem is None:
                return value
            console.print(f"  [yellow]{escape(problem)}[/yellow]")


_CANCELLED = object()


def _property(request: ElicitationRequest, item: ElicitationField) -> Any:
    return request.schema.get("properties", {}).get(item.name, {})


def _hint(item: ElicitationField) -> str:
    parts = []
    if item.kind == "boolean":
        parts.append("y/n")
    elif item.kind == "choices":
        parts.append("numbers, comma-separated")
    elif item.kind in ("integer", "number"):
        parts.append(item.kind)
    if item.default is not None:
        parts.append(f"default {item.default}")
    if not item.required:
        parts.append("optional")
    return f" ({', '.join(parts)})" if parts else ""
