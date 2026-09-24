from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from prompt_toolkit.formatted_text import AnyFormattedText
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from kerkov.agents.agent_session import AgentSession
from kerkov.cli.agent_display import (
    REVIEW_MARKER,
    agent_activity,
    agent_choice_label,
    agent_progress,
    format_elapsed,
    state_marker,
)
from kerkov.cli.agent_focus_manager import MASTER_FOCUS
from kerkov.cli.choice_styles import active_choice_label
from kerkov.cli.messages.user_message import UserMessage

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

console = Console()

MASTER_CHOICE_LABEL = "Main conversation"


class AgentsMessage(UserMessage):
    """`/agents` — list this session's agents, then switch focus, stop or review one.

    With a `target` (an id or a name) it attaches straight away; the picker is
    for when you do not already know which agent you want.
    """

    def __init__(
        self,
        target: Optional[str] = None,
        action: str = "attach",
        value: Optional[bool] = None,
    ):
        self.target = target
        self.action = action
        # `review` only: True/False set it outright, None toggles.
        self.value = value

    async def execute(self, cli: "KerkovCLI"):
        if self.action == "invalid":
            console.print(
                "[yellow]Usage: /agents stop <name|id>[/yellow] "
                "[dim]— name the agent; `/q` stops everything.[/dim]"
            )
            console.print(
                "[yellow]       /agents review <name|id> [on|off][/yellow] "
                "[dim]— review that agent's tool calls while attached.[/dim]"
            )
            console.print()
            return

        if self.action == "stop":
            await self._stop(cli, self.target or "")
            return
        if self.action == "review":
            self._review(cli, self.target or "")
            return
        if self.target is not None:
            self._switch_to(cli, self.target)
            return

        sessions = cli.agent_supervisor.list()
        if not sessions:
            console.print("[dim]No agents have run in this session.[/dim]")
            console.print(
                "[dim]Agents appear here once the assistant delegates work.[/dim]"
            )
            console.print()
            return

        console.print(_build_table(sessions))
        console.print()
        selected = await cli.input_manager.choose(
            "Switch focus",
            _build_options(cli, sessions),
            default=cli.agent_focus.current,
        )
        if selected is None:
            return
        self._switch_to(cli, selected)

    async def _stop(self, cli: "KerkovCLI", handle: str) -> None:
        """Ask one agent to stop; it still reports whatever it already found."""
        session = cli.agent_supervisor.get(handle)
        if session is None:
            _report_unknown(handle)
            return
        name = escape(session.display_name())
        if session.is_finished:
            console.print(f"[dim]{name} has already finished.[/dim]")
            console.print()
            return
        # Detach first: the view would otherwise keep streaming an agent the
        # user just told to stop, and the prompt would stay pointed at it.
        if session.id == cli.agent_focus.agent_id:
            cli.agent_attach.detach()
        console.print(f"[dim]Stopping {name}…[/dim]")
        await cli.agent_supervisor.cancel(session.id)
        status = session.report.status if session.report is not None else "unknown"
        console.print(
            f"[yellow]Stopped {name}[/yellow] "
            f"[dim]({status}) — its report arrives at your next turn.[/dim]"
        )
        console.print()

    def _review(self, cli: "KerkovCLI", handle: str) -> None:
        """Turn operator review of one agent's tool calls on or off."""
        session = cli.agent_supervisor.get(handle)
        if session is None:
            _report_unknown(handle)
            return
        name = escape(session.display_name())
        if session.is_finished:
            console.print(f"[dim]{name} has already finished.[/dim]")
            console.print()
            return

        session.review_tools = (
            not session.review_tools if self.value is None else self.value
        )
        if not session.review_tools:
            console.print(
                f"[dim]{name} is back to auto-approving within its tool "
                "whitelist.[/dim]"
            )
            console.print()
            return

        console.print(
            f"[yellow]Reviewing {name}'s tool calls.[/yellow] "
            "[dim]You are asked before each one that your approval mode would "
            "prompt for.[/dim]"
        )
        # Review needs somebody watching: the prompt is the agent's only way to
        # ask, and it is only pointed at one agent at a time.
        if cli.agent_focus.agent_id != session.id:
            console.print(
                f"[dim]It only applies while you are attached — `/agents "
                f"{name}` to watch it.[/dim]"
            )
        console.print()

    def _switch_to(self, cli: "KerkovCLI", handle: str) -> None:
        if handle == MASTER_FOCUS:
            if cli.agent_focus.is_master:
                console.print("[dim]Already on the main conversation.[/dim]")
                console.print()
                return
            cli.agent_attach.detach()
            return

        session = cli.agent_supervisor.get(handle)
        if session is None:
            _report_unknown(handle)
            return
        if session.id == cli.agent_focus.agent_id:
            console.print(
                f"[dim]Already attached to {escape(session.display_name())}.[/dim]"
            )
            console.print()
            return
        cli.agent_attach.attach(session)


class AgentReportWakeMessage(UserMessage):
    """A detached agent reported while the prompt sat idle — act on it now.

    Carries no text: the turn's content is whatever `get_response` delivers at
    its own turn boundary. Without this the findings would sit in the queue
    until the operator happened to type something.
    """

    async def execute(self, cli: "KerkovCLI") -> None:
        await cli.get_response()


class BackMessage(UserMessage):
    """`/back` — return focus to the main conversation."""

    async def execute(self, cli: "KerkovCLI"):
        if cli.agent_focus.is_master:
            console.print("[dim]Already on the main conversation.[/dim]")
            console.print()
            return
        cli.agent_attach.detach()


def _report_unknown(handle: str) -> None:
    console.print(f"[yellow]No agent named {escape(handle)}.[/yellow]")
    console.print("[dim]Run `/agents` to see this session's agents.[/dim]")
    console.print()


def _build_table(sessions: Sequence[AgentSession]) -> Table:
    spent = sum(s.tokens_used() for s in sessions)
    table = Table(
        title="Agents",
        # Agent spend never reaches the master's token counter, so this line is
        # the only place the operator sees what delegation has cost.
        caption=f"{spent:,} tokens billed by agents this session" if spent else None,
        border_style="dim",
    )
    table.add_column("", no_wrap=True)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Profile", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Elapsed", justify="right", style="dim")
    table.add_column("Iter", justify="right", style="dim")
    table.add_column("Tokens", justify="right", style="dim")
    table.add_column("Task", max_width=40)

    for session in sessions:
        tokens = session.tokens_used()
        table.add_row(
            state_marker(session),
            session.display_name(),
            session.id,
            session.profile,
            agent_activity(session)
            + (f" · {REVIEW_MARKER}" if session.review_tools else ""),
            format_elapsed(session.elapsed()),
            agent_progress(session).replace("iter ", "") or "[dim]—[/dim]",
            f"{tokens:,}" if tokens else "[dim]—[/dim]",
            session.task,
        )
    return table


def _build_options(
    cli: "KerkovCLI", sessions: Sequence[AgentSession]
) -> List[Tuple[str, AnyFormattedText]]:
    focused = cli.agent_focus.current
    options: List[Tuple[str, AnyFormattedText]] = [
        (
            MASTER_FOCUS,
            (
                active_choice_label(MASTER_CHOICE_LABEL)
                if focused == MASTER_FOCUS
                else MASTER_CHOICE_LABEL
            ),
        )
    ]
    for session in sessions:
        label = agent_choice_label(session)
        options.append(
            (
                session.id,
                active_choice_label(label) if session.id == focused else label,
            )
        )
    return options
