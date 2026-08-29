"""The attached view: one agent's output streamed into the master's terminal.

Attaching is read-only in this stage — the view mirrors what the agent's
`QuietSink` recorded and nothing typed reaches it. Replay-then-stream (rather
than stream-only) is what makes attaching to an agent that started ten minutes
ago useful at all.
"""

from typing import TYPE_CHECKING, Optional

from rich.console import Console
from rich.markup import escape

from kerkov.agents.agent_session import AgentSession
from kerkov.cli.agent_display import agent_activity, agent_prompt_label
from kerkov.core.logging.logger_factory import get_logger

if TYPE_CHECKING:
    from kerkov.agents.agent_supervisor import AgentSupervisor
    from kerkov.cli.agent_focus_manager import AgentFocusManager

logger = get_logger("AgentAttachView")

# A long-running agent's buffer is thousands of lines; a wall of scrollback on
# attach buries the live output that follows it.
REPLAY_TAIL_LINES = 200
# `QuietSink.tool_start` records the whole params dict, which for a `report`
# call is the entire findings body. Trim what the view prints, not the buffer:
# the agent log still has all of it. Matches `ConsoleSink`'s master-side cap.
STREAM_LINE_CHARS = 1000
STREAM_PREFIX = "│ "


class AgentAttachView:
    """Renders one focused agent, and owns the subscription that feeds it."""

    def __init__(
        self,
        console: Console,
        supervisor: "AgentSupervisor",
        focus: "AgentFocusManager",
    ) -> None:
        self._console = console
        self._supervisor = supervisor
        self._focus = focus
        self._cursor = 0
        self._subscribed = False

    def attach(self, session: AgentSession) -> None:
        """Focus one agent: replay what it has said, then follow it live."""
        self._unsubscribe()
        self._focus.focus_agent(session.id)
        self._console.print()
        self._console.print(
            f"[bold cyan]Attached to {escape(agent_prompt_label(session))}[/bold cyan] "
            f"[dim]· id {session.id} · {escape(agent_activity(session))}[/dim]",
            highlight=False,
        )
        self._console.print(f"[dim]Task: {escape(session.task)}[/dim]", highlight=False)
        self._replay(session)
        if session.is_finished:
            self._console.print(
                f"[dim]{escape(session.display_name())} has finished; "
                "this is its recorded output.[/dim]",
                highlight=False,
            )
        self._console.print(
            "[dim]Read-only — typed input reaches agents in a later build. "
            "`/back` or `/agents` to return.[/dim]"
        )
        self._console.print()
        self._supervisor.subscribe(self._on_event)
        self._subscribed = True

    def detach(self) -> Optional[str]:
        """Return focus to the master; returns the id it was attached to."""
        agent_id = self._focus.agent_id
        self._unsubscribe()
        self._focus.focus_master()
        if agent_id is not None:
            self._console.print("[dim]Back on the main conversation.[/dim]")
            self._console.print()
        return agent_id

    def _replay(self, session: AgentSession) -> None:
        buffer = session.events.buffer
        start = max(0, len(buffer) - REPLAY_TAIL_LINES)
        if start:
            self._console.print(
                f"[dim]… {start} earlier line(s) omitted; full transcript in the "
                "agent log.[/dim]"
            )
        for line in buffer[start:]:
            self._print_stream_line(line)
        self._cursor = len(buffer)

    def _on_event(self, session: AgentSession, event: str) -> None:
        if session.id != self._focus.agent_id:
            return
        if event == "output":
            self._drain(session)
        elif event == "state":
            self._drain(session)
            banner = escape(f"{STREAM_PREFIX}[{agent_activity(session)}]")
            self._console.print(f"[dim cyan]{banner}[/dim cyan]", highlight=False)

    def _drain(self, session: AgentSession) -> None:
        buffer = session.events.buffer
        # The buffer only grows, but a re-attach resets the cursor, so clamp
        # rather than trusting it to be in range.
        if self._cursor > len(buffer):
            self._cursor = len(buffer)
        for line in buffer[self._cursor :]:
            self._print_stream_line(line)
        self._cursor = len(buffer)

    def _print_stream_line(self, line: str) -> None:
        for part in line.splitlines() or [""]:
            if len(part) > STREAM_LINE_CHARS:
                part = part[:STREAM_LINE_CHARS] + "…"
            # highlight=False or Rich re-colours the agent's own text — numbers,
            # quoted strings and IPs light up mid-sentence and the view reads
            # as syntax, not as output.
            self._console.print(
                f"[dim]{STREAM_PREFIX}{escape(part)}[/dim]", highlight=False
            )

    def _unsubscribe(self) -> None:
        if self._subscribed:
            self._supervisor.unsubscribe(self._on_event)
            self._subscribed = False
        self._cursor = 0
