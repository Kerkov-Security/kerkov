"""`/workflow` in the web console: list workflows and start or stop CTF.

Compliance runs start from its panel, which collects the plan they need, so the
typed command only points there.
"""

from __future__ import annotations

import shlex
from typing import Any

from kerkov.core.workflows.registry import (
    CTF_WORKFLOW,
    WORKFLOW_DEFINITIONS,
    find_workflow,
    mode_mismatch_notice,
    workflow_names,
)
from kerkov.web.runtime.events import WebEventBus

CTF_USAGE = "Use /workflow ctf, /workflow ctf start or /workflow ctf stop."


def _result(
    bus: WebEventBus, session: Any, title: str, detail: str, *, failed: bool = False
) -> None:
    bus.emit(
        "workflow.result",
        {
            "title": title,
            "detail": detail,
            "failed": failed,
            "workflow": session.engagement_store.workflow,
        },
    )


def run_workflow_command(session: Any, message: str, bus: WebEventBus) -> None:
    """Answer one typed `/workflow` command with a `workflow.result` event."""
    try:
        args = shlex.split(message)[1:]
    except ValueError as exc:
        _result(bus, session, "Invalid workflow command", str(exc), failed=True)
        return
    if not args or (len(args) == 1 and args[0].lower() == "list"):
        lines = [
            f"{d.name} ({d.mode_label}): {d.summary}" for d in WORKFLOW_DEFINITIONS
        ]
        _result(bus, session, "Workflows", "\n".join(lines))
        return
    definition = find_workflow(args[0])
    if definition is None:
        _result(
            bus,
            session,
            "Unknown workflow",
            f"Unknown workflow {args[0]!r}. Available: {', '.join(workflow_names())}.",
            failed=True,
        )
        return
    if definition is not CTF_WORKFLOW:
        _result(
            bus,
            session,
            f"{definition.title} runs from its panel",
            f"Open {definition.title} under Workflows to set up and start a run.",
            failed=True,
        )
        return
    _ctf(session, [arg.lower() for arg in args[1:]], bus)


def _ctf(session: Any, args: list[str], bus: WebEventBus) -> None:
    action = args[0] if args else "status"
    if len(args) > 1 or action not in {"status", "start", "stop"}:
        _result(bus, session, "Invalid workflow command", CTF_USAGE, failed=True)
        return
    store = session.engagement_store
    active = store.workflow == CTF_WORKFLOW.name
    if action == "status":
        state = "active" if active else "not active"
        _result(bus, session, f"CTF objective {state}", "")
        return
    if action == "stop":
        if active:
            store.update_workflow(None)
        _result(
            bus,
            session,
            "CTF objective stopped" if active else "CTF is not active",
            "Later turns run under the engagement's mode alone." if active else "",
        )
        return
    if active:
        _result(bus, session, "CTF is already active", "")
        return
    session.engagement_workspace()
    if store.engagement_id is None:
        _result(
            bus,
            session,
            "CTF needs an engagement",
            "Choose a configured provider first; the objective is saved with "
            "the engagement it starts.",
            failed=True,
        )
        return
    store.update_workflow(CTF_WORKFLOW.name)
    notice = mode_mismatch_notice(
        CTF_WORKFLOW, session.mode_manager.current_mode.mode_type
    )
    detail = (
        "Every turn in this engagement now carries the capture-the-flag "
        "objective. Name the challenge or target to begin."
    )
    _result(
        bus,
        session,
        "CTF objective active",
        f"{detail}\n{notice}" if notice else detail,
    )
