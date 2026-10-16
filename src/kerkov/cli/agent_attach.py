"""The attached view: one agent's output streamed into the master's terminal.

The view mirrors what the agent's `QuietSink` recorded; what the user types at
the prompt is queued on the agent's inbox and read at its next loop boundary.
Replay-then-stream (rather than stream-only) is what makes attaching to an
agent that started ten minutes ago useful at all.

Being attached is also what makes tool review possible: `/agents review` only
takes effect for the agent on screen, because the prompt is the only place its
question can be asked.

There is no prompt while the master's turn is running, so a `Ctrl-O` attach
mid-turn is watch-only until that turn ends — a second concurrent input surface
would re-open the stdin-ownership hazard the mid-turn switcher just closed.
"""

from typing import TYPE_CHECKING, Optional

from rich.console import Console
from rich.markup import escape

from kerkov.agents.agent_session import AgentSession
from kerkov.cli.agent_display import (
    agent_activity,
    agent_model_label,
    agent_prompt_label,
)
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.process.ansi_strip import strip_ansi

if TYPE_CHECKING:
    from kerkov.agents.agent_supervisor import AgentSupervisor
    from kerkov.cli.agent_focus_manager import AgentFocusManager
    from kerkov.cli.buffering_sink import BufferingSink

logger = get_logger("AgentAttachView")

# A long-running agent's buffer is thousands of lines; a wall of scrollback on
# attach buries the live output that follows it.
REPLAY_TAIL_LINES = 200
# `QuietSink.tool_start` records the whole params dict, which for a `report`
# call is the entire findings body. Trim what the view prints, not the buffer:
# the agent log still has all of it. Matches `ConsoleSink`'s master-side cap.
STREAM_LINE_CHARS = 1000
# The stream is the one console path that prints while the chat prompt is live,
# so `patch_stdout(raw=True)` no longer scrubs ESC on its way out — and these
# lines carry target-controlled bytes (an HTTP body, a file read off a host).
# `strip_ansi` in `_print_stream_line` is what keeps that out of the terminal.
STREAM_PREFIX = "│ "


class AgentAttachView:
    """Renders one focused agent, and owns the subscription that feeds it."""

    def __init__(
        self,
        console: Console,
        supervisor: "AgentSupervisor",
        focus: "AgentFocusManager",
        output_sink: Optional["BufferingSink"] = None,
    ) -> None:
        self._console = console
        self._supervisor = supervisor
        self._focus = focus
        self._output_sink = output_sink
        self._cursor = 0
        self._subscribed = False
        self._pending_header_lines: set[str] = set()

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
        selection = agent_model_label(session)
        if selection:
            line = f"Model: {selection}"
            self._print_stream_line(line)
            self._pending_header_lines.add(line)
        if session.routing_reason:
            line = f"Routing: {session.routing_reason}"
            self._print_stream_line(line)
            self._pending_header_lines.add(line)
        self._replay(session)
        if session.is_finished:
            self._console.print(
                f"[dim]{escape(session.display_name())} has finished; "
                "this is its recorded output. `/back` or `/agents` to "
                "return.[/dim]",
                highlight=False,
            )
        else:
            name = escape(session.display_name())
            self._console.print(
                "[dim]What you type at the next prompt is queued for this agent "
                "and read at its next loop boundary. `/back` to return, "
                f"`/agents stop {name}` to stop it.[/dim]"
            )
            if session.review_tools:
                self._console.print(
                    "[dim]Reviewing its tool calls — you are asked before each "
                    f"one. `/agents review {name} off` to stop.[/dim]"
                )
            else:
                self._console.print(
                    f"[dim]`/agents review {name}` to approve its tool calls "
                    "yourself while you watch.[/dim]"
                )
        self._console.print()
        # The master's turn keeps running underneath; hold its narration back
        # so two live conversations do not interleave line by line.
        if self._output_sink is not None:
            self._output_sink.start_buffering()
        self._supervisor.subscribe(self._on_event)
        self._subscribed = True

    def detach(self) -> Optional[str]:
        """Return focus to the master; returns the id it was attached to."""
        agent_id = self._focus.agent_id
        self._unsubscribe()
        self._focus.focus_master()
        self._flush_master_output()
        if agent_id is not None:
            self._console.print("[dim]Back on the main conversation.[/dim]")
            self._console.print()
        return agent_id

    def _flush_master_output(self) -> None:
        """Replay what the master said while the terminal was elsewhere."""
        if self._output_sink is None or not self._output_sink.buffering:
            return
        if self._output_sink.pending:
            self._console.print()
            self._console.print("[dim]— main conversation while you were away —[/dim]")
        self._output_sink.flush()

    def _replay(self, session: AgentSession) -> None:
        buffer = session.events.buffer
        start = max(0, len(buffer) - REPLAY_TAIL_LINES)
        if start:
            self._console.print(
                f"[dim]… {start} earlier line(s) omitted; full transcript in the "
                "agent log.[/dim]"
            )
        for line in buffer[start:]:
            self._print_agent_line(line)
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
            self._print_agent_line(line)
        self._cursor = len(buffer)

    def _print_agent_line(self, line: str) -> None:
        # A queued agent can emit its initial metadata after attach, so share
        # this one-time suppression between replay and the live stream.
        if line in self._pending_header_lines:
            self._pending_header_lines.remove(line)
            return
        self._print_stream_line(line)

    def _print_stream_line(self, line: str) -> None:
        for part in line.splitlines() or [""]:
            if len(part) > STREAM_LINE_CHARS:
                part = part[:STREAM_LINE_CHARS] + "…"
            # highlight=False or Rich re-colours the agent's own text — numbers,
            # quoted strings and IPs light up mid-sentence and the view reads
            # as syntax, not as output.
            self._console.print(
                f"[dim]{STREAM_PREFIX}{escape(strip_ansi(part))}[/dim]",
                highlight=False,
            )

    def _unsubscribe(self) -> None:
        if self._subscribed:
            self._supervisor.unsubscribe(self._on_event)
            self._subscribed = False
        self._cursor = 0
        self._pending_header_lines.clear()
