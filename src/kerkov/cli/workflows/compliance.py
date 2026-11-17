"""Terminal front end for the compliance workflow."""

import asyncio
import copy
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import ValidationError
from rich.markup import escape

from kerkov.cli.workflows.render import SHORT_ID, run_view, runs_table
from kerkov.core.compliance.baselines import builtin_baselines
from kerkov.core.compliance.workflows import WorkflowRequest, WorkflowStore
from kerkov.core.conversation_engine import OutputSink
from kerkov.core.knowledge_base.models import (
    RESOURCE_SECTIONS,
    LedgerEntry,
    LedgerSection,
)
from kerkov.core.knowledge_base.ssh_access import project_ssh_access
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.run_workspace import get_current_workspace
from kerkov.core.workflows.compliance import ComplianceRun, merge_history
from kerkov.core.workflows.context import WorkflowContext, WorkflowObserver
from kerkov.core.workflows.registry import WorkflowDefinition, mode_mismatch_notice

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI

logger = get_logger(__name__)
USAGE = (
    "Use /workflow compliance runs, show <run-id>, assess, preview, "
    "apply <run-id> or reassess <run-id>."
)
MIN_PREFIX = 6
CUSTOM = "custom"
MANUAL = "manual"


class SetupCancelled(Exception):
    """The operator backed out of a setup prompt."""


class TerminalObserver(WorkflowObserver):
    """Narrate each stage change through the CLI's output sink."""

    def __init__(self, sink: OutputSink) -> None:
        self.sink = sink
        self.stage: Optional[str] = None

    def updated(self, projection: Dict[str, Any]) -> None:
        stage = projection.get("stage")
        # The finished record is rendered in full once the run returns.
        if stage != self.stage and stage != "finished":
            self.sink.info(
                escape(f"Compliance {stage}: {projection.get('detail', '')}")
            )
        self.stage = stage


def engagement_workspace() -> Path:
    workspace = get_current_workspace()
    if workspace is None:
        raise ValueError(
            "Start or resume an engagement first; compliance runs are saved in "
            "its workspace."
        )
    return workspace


def load_run(workspace: Path, token: str) -> Dict[str, Any]:
    """Load one run by its full ID or a unique prefix of a recent run's ID."""
    store = WorkflowStore(workspace)
    token = token.strip().lower()
    if re.fullmatch(r"[a-f0-9]{32}", token):
        return store.load(token)
    if not re.fullmatch(rf"[a-f0-9]{{{MIN_PREFIX},31}}", token):
        raise ValueError(
            f"Give a run ID, or at least {MIN_PREFIX} characters of one, from "
            "/workflow compliance runs."
        )
    matches = [record for record in store.list() if record["id"].startswith(token)]
    if not matches:
        raise ValueError(f"No recent compliance run starts with {token}.")
    if len(matches) > 1:
        raise ValueError(f"{token} matches several runs; give more characters.")
    return matches[0]


def validated(data: Dict[str, Any]) -> WorkflowRequest:
    try:
        return WorkflowRequest.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        field = ".".join(str(part) for part in error["loc"])
        raise ValueError(f"Invalid compliance settings ({field}): {error['msg']}")


def apply_request(record: Dict[str, Any]) -> WorkflowRequest:
    """The previewed run's own request, re-issued as an apply of that preview."""
    if record.get("status") != "previewed":
        raise ValueError(
            "Only a completed preview can be applied; this run is "
            f"{record.get('status')}. Preview again first."
        )
    data = copy.deepcopy(record["request"])
    data.update(action="apply", preview_id=record["id"], assessment_of=None)
    if data.get("remediation"):
        data["remediation"]["check"] = False
    return validated(data)


def reassess_request(record: Dict[str, Any]) -> WorkflowRequest:
    """A recovery assessment of a saved run, with its original asset and settings."""
    if not (record.get("plan") or {}).get("profile"):
        raise ValueError(
            "This run has no prepared profile to reassess. Start a new "
            "assessment with /workflow compliance assess."
        )
    data = copy.deepcopy(record["request"])
    data.update(
        action="assess",
        assessment_of=record["id"],
        preview_id=None,
        remediation=None,
        bootstrap_python=False,
    )
    return validated(data)


async def _choose(
    cli: "KerkovCLI", message: str, options: List[tuple], default: Optional[str] = None
) -> str:
    selected = await cli.input_manager.choose(message, options, default=default)
    if selected is None:
        raise SetupCancelled()
    return selected


async def _text(
    cli: "KerkovCLI", label: str, default: str = "", required: bool = True
) -> str:
    while True:
        try:
            value = await cli.input_manager.get_text(
                f"<b>{label}:</b> ", default=default
            )
        except (KeyboardInterrupt, EOFError):
            raise SetupCancelled()
        value = value.strip()
        if value or not required:
            return value
        cli.console.print(f"[yellow]{label} is required.[/yellow]")


def _host_label(row: LedgerEntry) -> str:
    entry = row.entry
    name = f" — {entry['name']}" if entry.get("name") else ""
    return f"#{row.id} {entry.get('value')}{name} ({row.section.value[:-1]})"


async def collect_request(cli: "KerkovCLI", action: str) -> WorkflowRequest:
    """Prompt for one assess or preview request, prefilled from the ledger."""
    ledger = cli.get_resource_ledger()
    if ledger is None:
        raise ValueError(
            "Record the host first with /asset add ip <address> (or a domain); "
            "compliance runs against a recorded asset."
        )
    rows = await ledger.list(sections=(*RESOURCE_SECTIONS, LedgerSection.CREDENTIALS))
    hosts = [
        row
        for row in rows
        if row.section in RESOURCE_SECTIONS
        and row.entry.get("kind") in {"ip", "domain"}
    ]
    if not hosts:
        raise ValueError(
            "No ip or domain asset is recorded. Add one with /asset add ip <address>."
        )
    host_id = await _choose(
        cli, "Host to assess", [(str(row.id), _host_label(row)) for row in hosts]
    )
    host = next(row for row in hosts if str(row.id) == host_id)

    baselines = builtin_baselines()
    baseline_id = await _choose(
        cli,
        "Baseline",
        [(b["id"], f"{b['name']} — {b['platform']}") for b in baselines]
        + [(CUSTOM, "Existing profile project (enter a path)")],
    )
    baseline = next((b for b in baselines if b["id"] == baseline_id), None)

    prefill: Dict[str, Any] = {}
    access = project_ssh_access(rows, host.id) or {}
    candidates = access.get("candidates", [])
    if candidates:
        options = [
            (
                str(index),
                "{user}@{host}:{port} key {key}".format(
                    user=c["fields"].get("user"),
                    host=host.entry.get("value"),
                    port=c["fields"].get("port", "?"),
                    key=c["fields"].get("private_key_file", "?"),
                ),
            )
            for index, c in enumerate(candidates)
        ]
        picked = await _choose(
            cli,
            "Recorded SSH access",
            options + [(MANUAL, "Enter connection details manually")],
        )
        if picked != MANUAL:
            prefill = candidates[int(picked)]["fields"]

    scan: Dict[str, Any] = {"host": host.entry["value"]}
    scan["user"] = await _text(cli, "SSH user", str(prefill.get("user", "")))
    port = await _text(cli, "SSH port", str(prefill.get("port", 22)))
    if not port.isdigit():
        raise ValueError("The SSH port must be a number.")
    scan["port"] = int(port)
    scan["private_key_file"] = await _text(
        cli, "Private key file", str(prefill.get("private_key_file", ""))
    )
    scan["known_hosts_file"] = await _text(
        cli, "Trusted known_hosts file", str(prefill.get("known_hosts_file", ""))
    )
    scan["sudo"] = (
        await _choose(
            cli,
            "Run checks with sudo?",
            [("yes", "Yes"), ("no", "No")],
            default="no" if prefill.get("sudo") is False else "yes",
        )
        == "yes"
    )

    remediation = None
    if baseline:
        scan["profile"] = baseline["profile"]
    else:
        scan["profile"] = await _text(cli, "Profile project directory")
        inputs = await _text(cli, "Inputs file (optional)", required=False)
        if inputs:
            scan["inputs_file"] = inputs

    bootstrap = False
    if action != "assess":
        bootstrap = (
            await _choose(
                cli,
                "Install Python 3 on the target first if it is missing?",
                [("no", "No"), ("yes", "Yes (Debian, Ubuntu or Alpine)")],
                default="no",
            )
            == "yes"
        )
        if not baseline:
            profile = await _text(cli, "Saved Ansible profile")
            playbook = await _text(cli, "Playbook")
            alias = await _text(cli, "Ansible host alias")
            if alias in {"all", "ungrouped"}:
                raise ValueError("Choose a single host alias, not an inventory group.")
            vars_file = await _text(cli, "Variables file (optional)", required=False)
            remediation = {
                "action": "run",
                "profile": profile,
                "playbook": playbook,
                "limit": alias,
                "check": action == "preview",
                "bootstrap_python": bootstrap,
                "vars_files": [vars_file] if vars_file else [],
            }

    return validated(
        {
            "action": action,
            "resource_id": host.id,
            "baseline_id": baseline["id"] if baseline else None,
            "scan": scan,
            "remediation": remediation,
            "bootstrap_python": bootstrap,
        }
    )


def build_context(cli: "KerkovCLI") -> WorkflowContext:
    """Bind a run to the CLI's live engagement, ledger and approval path."""
    return WorkflowContext(
        config=cli.config,
        workspace=get_current_workspace,
        context_id=lambda: cli.engagement_store.engagement_id,
        ledger=lambda: cli.get_resource_ledger(),
        approval=cli.interactive_approval,
        closed=lambda: False,
        sink=cli.output_sink,
        observer=TerminalObserver(cli.output_sink),
    )


async def start_run(
    cli: "KerkovCLI", definition: WorkflowDefinition, request: WorkflowRequest
) -> None:
    """Prepare, execute and record one gated run, then show its outcome."""
    run = ComplianceRun(build_context(cli), request)
    try:
        run.publish()
        await run.prepare()
        await cli.run_workflow_turn(
            run.plan_message(),
            approval=run,
            output=run.output,
            overlay=definition.overlay,
        )
    except Exception as exc:
        expected = isinstance(exc, (ValueError, OSError))
        if not expected:
            logger.exception(f"Compliance workflow failed: {exc}")
        run.failed = True
        run.record["detail"] = (
            str(exc)
            if expected
            else "Workflow failed. Review saved evidence and the session log."
        )
        if run.record["stage"] == "readiness":
            run.record["readiness"].append(
                {
                    "name": "Readiness",
                    "status": "blocked",
                    "detail": run.record["detail"],
                }
            )
    finally:
        await run.finish(cli.input_manager)
        cli.input_manager.reset_interrupt()
    cli.console.print(run_view(run.projection()))
    if run.record["status"] == "previewed":
        cli.console.print(
            "[dim]Review the outcomes above, then apply exactly this plan with "
            f"/workflow compliance apply {run.record['id'][:SHORT_ID]}.[/dim]"
        )


async def handle(
    cli: "KerkovCLI", definition: WorkflowDefinition, args: List[str]
) -> None:
    action = args[0].lower() if args else "runs"
    if action == "runs" and len(args) <= 1:
        workspace = engagement_workspace()
        records = merge_history(await asyncio.to_thread(WorkflowStore(workspace).list))
        if not records:
            cli.console.print("[dim]No compliance runs in this engagement.[/dim]")
            return
        cli.console.print(runs_table(records))
        cli.console.print(
            "[dim]Use /workflow compliance show <run-id> for controls and "
            "evidence.[/dim]"
        )
        return
    if action == "show" and len(args) == 2:
        workspace = engagement_workspace()
        record = await asyncio.to_thread(load_run, workspace, args[1])
        cli.console.print(run_view(merge_history([record])[0]))
        return

    runs = {"assess": 1, "preview": 1, "apply": 2, "reassess": 2}
    if runs.get(action) != len(args):
        raise ValueError(USAGE)
    if cli.tool_approval_helper.non_interactive_auto_approve:
        raise ValueError(
            "Run compliance workflows from the interactive CLI: --message "
            "auto-approves tool calls and cannot answer setup prompts."
        )
    if not cli.require_provider():
        return
    notice = mode_mismatch_notice(definition, cli.mode_manager.current_mode.mode_type)
    if notice:
        cli.console.print(f"[yellow]{escape(notice)}[/yellow]")
    if action in {"apply", "reassess"}:
        workspace = engagement_workspace()
        record = merge_history([await asyncio.to_thread(load_run, workspace, args[1])])[
            0
        ]
        request = (apply_request if action == "apply" else reassess_request)(record)
    else:
        try:
            request = await collect_request(cli, action)
        except SetupCancelled:
            cli.console.print("[dim]Compliance setup cancelled.[/dim]")
            return
    await start_run(cli, definition, request)
