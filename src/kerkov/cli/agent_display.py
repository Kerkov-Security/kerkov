"""One rendering of an agent session, shared by `/agents`, the picker, and attach.

The user addresses agents by name; the id rides along because reports and
`agent_wait` are keyed on it, so a name in the terminal has to be traceable to
what the model was told.
"""

from typing import Sequence

from prompt_toolkit.formatted_text import StyleAndTextTuples

from kerkov.agents.agent_session import AgentSession, AgentState
from kerkov.cli.agent_focus_manager import MASTER_FOCUS

ACTIVE_MARKER = "●"
FINISHED_MARKER = "○"
THINKING_LABEL = "thinking"
REVIEW_MARKER = "review"
MASTER_CHIP = "main"
STRIP_MAX_CHIPS = 4
STRIP_SEPARATOR = "  ·  "


def format_elapsed(seconds: float) -> str:
    """`00:42` / `12:07` — minutes and seconds, no hour rollover."""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def state_marker(session: AgentSession) -> str:
    return ACTIVE_MARKER if session.is_active else FINISHED_MARKER


def agent_activity(session: AgentSession) -> str:
    """What the agent is doing right now, or how it ended."""
    if session.is_active:
        # Only a running agent has a tool to name; queued/reporting/blocked are
        # each more informative than whatever tool happened to be last set.
        if session.state != AgentState.RUNNING:
            return session.state.value
        return session.current_tool or THINKING_LABEL
    if session.state == AgentState.DONE and session.report is not None:
        return f"done ({session.report.status})"
    return session.state.value


def agent_progress(session: AgentSession) -> str:
    """`iter 6/30`, or an empty string when the cap is unknown."""
    if not session.is_active or session.max_iterations is None:
        return ""
    return f"iter {session.iteration}/{session.max_iterations}"


def agent_choice_label(session: AgentSession) -> str:
    """One picker row: identity, task, then the live status trail."""
    trail = [agent_activity(session), format_elapsed(session.elapsed())]
    progress = agent_progress(session)
    if progress:
        trail.append(progress)
    if session.review_tools:
        trail.append(REVIEW_MARKER)
    selection = agent_model_label(session)
    if selection:
        trail.append(selection)
    if session.routing_reason:
        trail.append(session.routing_reason)
    return (
        f"{state_marker(session)} {session.display_name()}  {session.profile} "
        f"— {session.task}   · {' · '.join(trail)}"
    )


def agent_model_label(session: AgentSession) -> str:
    """The fixed provider/model selection as plain text, for picker and attach."""
    if not session.model:
        return ""
    label = (
        f"{session.provider} / {session.model}" if session.provider else session.model
    )
    if session.routing_tier:
        label += f" ({session.routing_tier})"
    return label


def agent_prompt_label(session: AgentSession) -> str:
    """`google-pinger (recon)` — the attached prompt's identity."""
    return f"{session.display_name()} ({session.profile})"


def agent_chip(session: AgentSession) -> str:
    """`● port-scan exec` — one agent, compressed to what changes."""
    chip = f"{state_marker(session)} {session.display_name()} {agent_activity(session)}"
    return f"{chip} [{REVIEW_MARKER}]" if session.review_tools else chip


def agent_strip(
    sessions: Sequence[AgentSession],
    *,
    focused: str = MASTER_FOCUS,
    max_chips: int = STRIP_MAX_CHIPS,
) -> StyleAndTextTuples:
    """The line under the prompt: where focus is, and what is running behind it.

    Returned as style/text fragments rather than markup so an agent name the
    model invented can never be read as formatting.
    """
    fragments: StyleAndTextTuples = [
        (
            "class:bottom-toolbar.focus" if focused == MASTER_FOCUS else "",
            MASTER_CHIP,
        )
    ]
    shown = list(sessions)[:max_chips]
    for session in shown:
        fragments.append(("", STRIP_SEPARATOR))
        style = "class:bottom-toolbar.focus" if session.id == focused else ""
        fragments.append((style, agent_chip(session)))
    remaining = len(sessions) - len(shown)
    if remaining > 0:
        fragments.append(("", f"{STRIP_SEPARATOR}+{remaining} more"))
    return fragments
