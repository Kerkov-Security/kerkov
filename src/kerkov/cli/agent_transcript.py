"""Reading a finished agent's transcript back off disk.

An agent's JSONL is a full conversation, but it is not a conversation the user
can continue: it belongs to a profile, a task and a parent engagement, and its
mode (`agent`) is not one the CLI can switch into. So `/resume <agent-id>`
opens it read-only rather than restoring it, and this is the renderer.

It is the only way to see an agent that finished in an earlier session — the
attached view replays a live `AgentSession`'s buffer, which dies with the CLI.
"""

from typing import List, Optional

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from kerkov.core.engagement.engagement_store import EngagementMetadata, EngagementStore
from kerkov.models.basic_models import Message

AGENT_KIND = "agent"

# Enough to see how a run ended without paging a 30-iteration transcript into
# the scrollback. The file keeps everything.
TRANSCRIPT_TAIL_MESSAGES = 40
MESSAGE_CHARS = 2000

_ROLE_STYLES = {
    "system": "dim",
    "user": "cyan",
    "assistant": "green",
    "tool": "magenta",
}


def render_agent_transcript(
    console: Console,
    metadata: EngagementMetadata,
    messages: List[Message],
    *,
    tail: int = TRANSCRIPT_TAIL_MESSAGES,
) -> None:
    """Print one agent run: what it was asked, then how it went."""
    console.print(Panel(_header(metadata), border_style="cyan", title="Agent Run"))

    start = max(0, len(messages) - tail)
    if start:
        console.print(
            f"[dim]… {start} earlier message(s) omitted; the full transcript is "
            f"in the engagement file.[/dim]"
        )
    for message in messages[start:]:
        _print_message(console, message)
    console.print()
    console.print(
        "[dim]Read-only: an agent run cannot be continued. `/resume <id>` on a "
        "conversation resumes it as usual.[/dim]"
    )
    console.print()


def show_agent_run_if_agent(
    console: Console, store: EngagementStore, engagement_id: str
) -> bool:
    """Print an agent transcript read-only; False when this is not one."""
    metadata = store.read_metadata(engagement_id)
    if metadata is None or metadata.kind != AGENT_KIND:
        return False

    # Loading changes a store's active engagement, and viewing is read-only.
    reader = EngagementStore(store_dir=store.store_dir)
    engagement = reader.load_engagement(engagement_id)
    messages = engagement.messages if engagement is not None else []
    render_agent_transcript(console, metadata, messages)
    return True


def _header(metadata: EngagementMetadata) -> str:
    lines = [
        f"[bold]{escape(metadata.agent_type or 'agent')}[/bold] "
        f"[dim]· {escape(metadata.id)}[/dim]",
    ]
    if metadata.task_instruction:
        lines.append(f"Task: {escape(metadata.task_instruction)}")
    if metadata.parent_engagement_id:
        lines.append(f"[dim]Parent: {escape(metadata.parent_engagement_id)}[/dim]")
    model = (
        f"{metadata.provider} / {metadata.model}"
        if metadata.provider
        else metadata.model
    )
    if metadata.routing_tier:
        model += f" ({metadata.routing_tier})"
    lines.append(f"[dim]Model: {escape(model)}[/dim]")
    if metadata.routing_reason:
        lines.append(f"[dim]Routing: {escape(metadata.routing_reason)}[/dim]")
    return "\n".join(lines)


def _print_message(console: Console, message: Message) -> None:
    style = _ROLE_STYLES.get(message.role, "white")
    console.print(f"[bold {style}]{message.role}[/bold {style}]", highlight=False)
    console.print(_body(message.content), highlight=False)
    console.print()


def _body(content: Optional[str]) -> str:
    text = content or ""
    if len(text) > MESSAGE_CHARS:
        text = text[:MESSAGE_CHARS] + "…"
    # escape, not markup: a tool result full of brackets is data, not styling.
    return escape(text)
