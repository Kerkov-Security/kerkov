"""One formatter for agent reports, shared by every delivery path.

`agent_wait` and the turn-boundary injector must render reports identically —
two formatters would drift and the model would learn two shapes for the same
fact. Entries carry the whole `AgentSession` so a report is headed by the name
the agent is known as, with the id kept alongside for anything addressing it.
"""

from typing import Dict, List, Optional, Sequence, Tuple

from kerkov.agents.agent_session import AgentSession
from kerkov.agents.models import AgentRunReport
from kerkov.tools.base_tool import ToolResult

ReportEntry = Tuple[AgentSession, AgentRunReport]


def _report_body(report: AgentRunReport) -> List[str]:
    body = ["", "FINDINGS:", report.findings]
    if report.next_steps:
        body.extend(["", "NEXT STEPS:", report.next_steps])
    return body


def _spend(report: AgentRunReport) -> str:
    """`, 12,345 tokens` — the master's only view of what a delegation cost.

    Agent usage never reaches the master's tracker, so if it is not in the
    report the master has no way to know fan-out has a price.
    """
    return f", {report.tokens_used:,} tokens" if report.tokens_used else ""


def _routing_metadata(session: AgentSession) -> Dict[str, str]:
    return {
        key: value
        for key, value in (
            ("provider", session.provider),
            ("model", session.model),
            ("routing_tier", session.routing_tier),
            ("routing_reason", session.routing_reason),
        )
        if value is not None
    }


def _routing_lines(session: AgentSession) -> List[str]:
    lines = []
    if session.model:
        selection = (
            f"{session.provider} / {session.model}"
            if session.provider
            else session.model
        )
        if session.routing_tier:
            selection += f" ({session.routing_tier})"
        lines.append(f"Model: {selection}")
    if session.routing_reason:
        lines.append(f"Routing: {session.routing_reason}")
    return lines


def format_report_section(session: AgentSession, report: AgentRunReport) -> str:
    """One report as it appears inside a multi-agent result."""
    lines = [
        f"### {session.display_name()} — {session.profile} "
        f"[{report.status}] (id: {session.id}{_spend(report)})"
    ]
    if report.engagement_id:
        lines.append(f"(engagement: {report.engagement_id})")
    lines.extend(_routing_lines(session))
    lines.extend(_report_body(report))
    return "\n".join(line for line in lines if line != "")


def format_report_sections(entries: Sequence[ReportEntry]) -> str:
    return "\n\n".join(format_report_section(s, r) for s, r in entries)


def format_report_for_injection(session: AgentSession, report: AgentRunReport) -> str:
    """A finished report as an out-of-band `user` message to the master."""
    lines = [
        f"Agent {session.display_name()} ({session.profile}, id: {session.id}"
        f"{_spend(report)}) reported [{report.status}]."
    ]
    if report.engagement_id:
        lines.append(f"(engagement: {report.engagement_id})")
    lines.extend(_routing_lines(session))
    lines.extend(_report_body(report))
    return "\n".join(line for line in lines if line != "")


def build_report_result(
    entries: Sequence[ReportEntry],
    *,
    still_running: Sequence[AgentSession] = (),
    unknown: Sequence[str] = (),
) -> ToolResult:
    """Concatenate reports into the single result the master consumes."""
    any_stuck = any(r.status == "stuck" for _, r in entries)
    sections = [format_report_sections(entries)] if entries else []

    if still_running:
        running = ", ".join(f"{s.display_name()} ({s.id})" for s in still_running)
        sections.append(f"Still running: {running}")
    if unknown:
        sections.append(f"Unknown agent(s): {', '.join(unknown)}")
    if not entries and not still_running and not unknown:
        sections.append("No agents to wait for — every report has been delivered.")

    error: Optional[str] = None
    if any_stuck:
        error = "One or more agents returned status=stuck"
    elif unknown and not entries:
        error = f"Unknown agent(s): {', '.join(unknown)}"

    return ToolResult(
        success=error is None,
        output="\n\n".join(sections),
        error=error,
        metadata={
            "reports": [
                {**report.model_dump(), **_routing_metadata(session)}
                for session, report in entries
            ],
            "agent_ids": [s.id for s, _ in entries],
            "agent_names": [s.display_name() for s, _ in entries],
            "any_stuck": any_stuck,
            "still_running": [s.id for s in still_running],
            "unknown": list(unknown),
        },
    )
