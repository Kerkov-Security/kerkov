"""One rendering of an agent session, shared by `/agents`, the picker, and attach.

The user addresses agents by name; the id rides along because reports and
`agent_wait` are keyed on it, so a name in the terminal has to be traceable to
what the model was told.
"""

from kerkov.agents.agent_session import AgentSession, AgentState

ACTIVE_MARKER = "●"
FINISHED_MARKER = "○"
THINKING_LABEL = "thinking"


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
    return (
        f"{state_marker(session)} {session.display_name()}  {session.profile} "
        f"— {session.task}   · {' · '.join(trail)}"
    )


def agent_prompt_label(session: AgentSession) -> str:
    """`google-pinger (recon)` — the attached prompt's identity."""
    return f"{session.display_name()} ({session.profile})"
