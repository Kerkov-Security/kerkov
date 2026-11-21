"""Expand an MCP server's prompt template into the operator's next message.

Prompts are user-controlled: the operator picks one with `/mcp prompt`, and its
rendered messages become an ordinary user turn. They are listed live, never
advertised to the model, so they cannot move the cached prompt prefix.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Dict, Mapping, Optional, Sequence

from kerkov.core.mcp.connection import McpCallError
from kerkov.core.mcp.manager import McpManager, PromptArgument, PromptInfo
from kerkov.tools.mcp.results import render_prompt

AskArgument = Callable[[PromptArgument], Awaitable[Optional[str]]]


class PromptError(ValueError):
    """The prompt cannot be expanded as asked; the message says why."""


def parse_prompt_arguments(tokens: Sequence[str]) -> Dict[str, str]:
    """`name=value` tokens, as `/mcp prompt <server> <prompt> [name=value ...]` takes."""
    arguments: Dict[str, str] = {}
    for token in tokens:
        name, separator, value = token.partition("=")
        if not separator or not name:
            raise PromptError(f"expected name=value, got {token!r}")
        arguments[name] = value
    return arguments


def describe_prompt(prompt: PromptInfo) -> str:
    """`name(required*, optional)`: description, one line per prompt."""
    arguments = ", ".join(
        arg.name + ("*" if arg.required else "") for arg in prompt.arguments
    )
    line = f"{prompt.name}({arguments})"
    return f"{line}: {prompt.description}" if prompt.description else line


async def expand_prompt(
    manager: McpManager,
    server: str,
    name: str,
    arguments: Mapping[str, str],
    *,
    ask: Optional[AskArgument] = None,
) -> str:
    """Fetch and render one prompt; `ask` supplies required arguments not given."""
    listing = await manager.list_prompts(server)
    if server in listing.errors:
        raise PromptError(f"{server}: {listing.errors[server]}")
    prompt = next((p for p in listing.prompts if p.name == name), None)
    if prompt is None:
        raise PromptError(
            f"{server} has no prompt named {name!r}; `/mcp prompts {server}` "
            "lists them"
        )
    known = {arg.name for arg in prompt.arguments}
    unknown = sorted(set(arguments) - known)
    if unknown:
        raise PromptError(
            f"{name} takes no argument {', '.join(unknown)}; it takes "
            + (", ".join(sorted(known)) or "none")
        )
    values = dict(arguments)
    for arg in prompt.arguments:
        if not arg.required or values.get(arg.name):
            continue
        value = await ask(arg) if ask is not None else None
        if not value:
            raise PromptError(f"{name} needs {arg.name}=<value>")
        values[arg.name] = value
    try:
        result = await manager.get_prompt(server, name, values)
    except McpCallError as exc:
        raise PromptError(str(exc)) from exc
    text = render_prompt(result, server_key=server)
    if not text.strip():
        raise PromptError(f"{server} returned an empty prompt for {name}")
    return text
