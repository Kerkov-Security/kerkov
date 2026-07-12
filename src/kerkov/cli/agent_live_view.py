"""AgentLiveView — the live region owner for a `delegate` batch (issue #148).

Scope (Stage 0/1 PoC): own one Rich `Live` region for the duration of a
`delegate` batch and render one status row per running agent — profile,
task, current tool / iteration, elapsed, status — driven entirely by
`AgentEvent`s off an `AgentEventBus`. It restores the normal console on exit
and is a no-op-ish plain-print fallback when stdout is not a TTY (tests,
piped output).

It is deliberately NOT a general terminal coordinator. Its only jobs are:
own the `Live` while agents run, repaint rows from bus events, and hand the
console back untouched. Attach/detach + a switcher hang off this same seam
in later stages; this PoC ships the read-only status panel.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from kerkov.agents.agent_events import (
    AgentEvent,
    AgentEventBus,
    AgentRowState,
    build_rows,
)

_STATUS_STYLE = {
    "starting": "yellow",
    "running": "cyan",
    "completed": "green",
    "partial": "yellow",
    "stuck": "red",
    "error": "red",
    "finished": "green",
}


def _fmt_elapsed(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


class AgentLiveView:
    """Renders a live per-agent status panel for one `delegate` batch.

    Usage::

        bus = AgentEventBus()
        view = AgentLiveView(bus, console, specs=[(run_id, type, task), ...])
        with view.live():
            results = await asyncio.gather(*agent_coros)

    Pre-seeding rows from `specs` means an agent shows up as "starting"
    immediately, before it emits its first event.
    """

    def __init__(
        self,
        bus: AgentEventBus,
        console: Optional[Console] = None,
        *,
        specs: Optional[List[tuple]] = None,
        refresh_per_second: int = 4,
    ) -> None:
        self._bus = bus
        self._console = console or Console()
        self._rows: Dict[str, AgentRowState] = {}
        self._refresh_per_second = refresh_per_second
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._live: Optional[Live] = None
        for spec in specs or []:
            run_id, agent_type, task = spec
            self._rows[run_id] = AgentRowState(
                agent_run_id=run_id, agent_type=agent_type, task=task
            )

    # -- event handling -------------------------------------------------

    def on_event(self, event: AgentEvent) -> None:
        """Fold a bus event into row state and repaint.

        Rows are created lazily for any run id we didn't pre-seed, so the
        view is robust even if `specs` was incomplete.
        """
        row = self._rows.get(event.agent_run_id)
        if row is None:
            row = AgentRowState(
                agent_run_id=event.agent_run_id,
                agent_type=event.agent_type,
                task="",
            )
            self._rows[event.agent_run_id] = row
        if not row.agent_type and event.agent_type:
            row.agent_type = event.agent_type
        row.apply(event)
        self._repaint()

    # -- rendering ------------------------------------------------------

    def render(self) -> Table:
        """Build the current status table (pure; used by rendering + tests)."""
        table = Table(
            title="Delegated agents",
            title_style="bold",
            expand=True,
            show_edge=True,
        )
        table.add_column("Agent", style="bold", no_wrap=True)
        table.add_column("Task", overflow="ellipsis", max_width=48)
        table.add_column("Now", no_wrap=True)
        table.add_column("Iter", justify="right", no_wrap=True)
        table.add_column("Elapsed", justify="right", no_wrap=True)
        table.add_column("Status", no_wrap=True)

        for row in build_rows(self._rows):
            now_cell = row.current_tool or ("—" if row.finished else "thinking…")
            status_style = _STATUS_STYLE.get(row.status, "white")
            table.add_row(
                row.agent_type,
                row.task or Text("—", style="dim"),
                now_cell,
                str(row.iterations),
                _fmt_elapsed(row.elapsed()),
                Text(row.status, style=status_style),
            )
        return table

    def _repaint(self) -> None:
        if self._live is not None:
            self._live.update(self.render())

    # -- lifecycle ------------------------------------------------------

    def live(self) -> "AgentLiveView":
        """Context manager: subscribe to the bus and own a Live region.

        On a non-TTY console we skip `Live` (which would spam redraws into a
        pipe) and just subscribe so row state is still tracked; a one-shot
        summary is printed on exit for logs/tests.
        """
        return self

    def __enter__(self) -> "AgentLiveView":
        self._unsubscribe = self._bus.subscribe(self.on_event)
        if self._console.is_terminal:
            self._live = Live(
                self.render(),
                console=self._console,
                refresh_per_second=self._refresh_per_second,
                transient=True,
            )
            self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            # Final repaint so the last state is captured before teardown.
            self._live.update(self.render())
            self._live.__exit__(exc_type, exc, tb)
            self._live = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        # Leave a compact, non-transient summary behind in the scrollback.
        self._print_summary()

    def _print_summary(self) -> None:
        for row in build_rows(self._rows):
            style = _STATUS_STYLE.get(row.status, "white")
            self._console.print(
                f"[dim]agent[/dim] [bold]{row.agent_type}[/bold] "
                f"[{style}]{row.status}[/{style}] "
                f"[dim]({row.iterations} iters, {_fmt_elapsed(row.elapsed())})[/dim]"
            )
