"""Adapt the core compliance workflow to this browser session's events."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kerkov.core.automation.ansible import ansible_binary
from kerkov.core.automation.profiles import local_file
from kerkov.core.compliance.openscap import inspect_datastream
from kerkov.core.compliance.review import ReviewEvidence, export_report, review_control
from kerkov.core.compliance.workflows import WorkflowRequest, WorkflowStore
from kerkov.core.workflows.compliance import (
    ComplianceRun,
    controller_blockers,
    merge_history,
)
from kerkov.core.workflows.compliance import read_evidence as read_artifact
from kerkov.core.workflows.context import WorkflowContext, WorkflowObserver
from kerkov.core.workflows.registry import COMPLIANCE_WORKFLOW, mode_mismatch_notice
from kerkov.web.runtime.events import WebEventBus, WorkflowEventBus
from kerkov.web.runtime.output import WebOutputSink

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession


class WebWorkflowObserver(WorkflowObserver):
    """Publish run projections to the browser's compliance panel."""

    def __init__(self, bus: WebEventBus, context_id: Any, message_id: str) -> None:
        self.bus = bus
        self.context_id = context_id
        self.message_id = message_id

    def updated(self, projection: dict[str, Any]) -> None:
        self.bus.emit(
            "compliance.updated",
            {
                "context_id": self.context_id,
                "message_id": self.message_id,
                "run": projection,
            },
        )


def compliance_readiness(session: WebKerkovSession) -> dict[str, Any]:
    """What the compliance view shows before a run; `prepare()` still enforces it."""
    return {
        "blockers": controller_blockers(session.config),
        "mode_notice": mode_mismatch_notice(
            COMPLIANCE_WORKFLOW, session.mode_manager.current_mode.mode_type
        ),
    }


def build_run(
    session: WebKerkovSession, request: dict[str, Any], message_id: str
) -> ComplianceRun:
    """Build a gated compliance run bound to this session's live state."""

    panel = WorkflowEventBus(session.bus, message_id)
    context = WorkflowContext(
        config=session.config,
        workspace=lambda: session.workspace_path,
        context_id=lambda: session.inventory.context_id,
        ledger=lambda: session.findings_ledger,
        approval=lambda: session.approval_strategy,
        closed=lambda: session._closed,
        sink=WebOutputSink(panel, stream=False),
        observer=WebWorkflowObserver(
            session.bus, session.inventory.context_id, message_id
        ),
    )
    return ComplianceRun(context, WorkflowRequest.model_validate(request))


async def list_runs(session: WebKerkovSession, content: dict[str, Any]) -> None:
    context_id = session.inventory.context_id
    workspace = session.workspace_path
    if content.get("context_id") != context_id or workspace is None:
        return
    requested_run = session.compliance_run
    records = await asyncio.to_thread(WorkflowStore(workspace).list)
    if (
        session._closed
        or context_id != session.inventory.context_id
        or workspace != session.workspace_path
    ):
        return
    active = session.compliance_run or requested_run
    projection = (
        active.projection()
        if active is not None and active.context_id == context_id
        else None
    )
    session.bus.emit(
        "compliance.snapshot",
        {"context_id": context_id, "runs": merge_history(records, projection)},
    )


async def read_evidence(session: WebKerkovSession, content: dict[str, Any]) -> None:
    context_id = session.inventory.context_id
    workspace = session.workspace_path
    if content.get("context_id") != context_id or workspace is None:
        return
    try:
        body = await asyncio.to_thread(
            read_artifact,
            workspace,
            content.get("run_id", ""),
            content.get("phase"),
        )
    except (OSError, ValueError, TypeError):
        body = "Evidence could not be read. Inspect the saved artifact references and refresh the assessment history."
    if (
        not session._closed
        and context_id == session.inventory.context_id
        and workspace == session.workspace_path
    ):
        session.bus.emit(
            "compliance.evidence",
            {"context_id": context_id, "run_id": content.get("run_id"), "body": body},
        )


async def read_compliance_details(
    session: WebKerkovSession,
    content: dict[str, Any],
    evidence: ReviewEvidence | None = None,
) -> None:
    """Serve local inspection and review requests for the active engagement."""
    context_id = session.inventory.context_id
    workspace = session.workspace_path
    if content.get("context_id") != context_id or workspace is None:
        return
    request_id = content.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
        return
    event_type = content["type"]

    def read() -> dict[str, Any]:
        if event_type == "compliance.datastream.inspect":
            path = content.get("path")
            if (
                not isinstance(path, str)
                or not 1 <= len(path) <= 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
            ):
                raise ValueError("Enter a local datastream path")
            sha256, profiles = inspect_datastream(local_file(path, workspace))
            return {
                "sha256": sha256,
                "profiles": profiles,
                "ansible_available": ansible_binary() is not None,
            }
        record = WorkflowStore(workspace).load(content.get("run_id", ""))
        if event_type == "compliance.export":
            return export_report(
                workspace, record, content.get("format", ""), evidence=evidence
            )
        profile, control_id = content.get("profile"), content.get("control_id")
        if not all(
            isinstance(value, str) and 1 <= len(value) <= 1024
            for value in (profile, control_id)
        ):
            raise ValueError("Select a control and its profile")
        return {
            "control": review_control(
                workspace, record, profile, control_id, evidence=evidence
            )
        }

    try:
        result = await asyncio.to_thread(read)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        subject = (
            "inspect the datastream"
            if event_type == "compliance.datastream.inspect"
            else "load the saved assessment"
        )
        result = {"detail": f"Could not {subject}: {exc}"}
    if (
        not session._closed
        and context_id == session.inventory.context_id
        and workspace == session.workspace_path
    ):
        session.bus.emit(
            event_type,
            {
                "context_id": context_id,
                "run_id": content.get("run_id"),
                "request_id": request_id,
                **result,
            },
        )
