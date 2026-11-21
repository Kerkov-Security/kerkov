"""Web session runtime for connecting Django to Kerkov's core engine."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any, Optional, Sequence

from pydantic import ValidationError

from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.agents.report_format import format_report_for_injection
from kerkov.context.context_compactor import ContextCompactor
from kerkov.core.cancellable_task import (
    EventInterrupt,
    InterruptedByUser,
    run_cancellable,
)
from kerkov.core.commands import Intent, parse
from kerkov.core.compliance.profile_sources import ProfileSource
from kerkov.core.config_manager import (
    TRUSTED_SOURCE_SETTINGS,
    ConfigManager,
    get_config,
)
from kerkov.core.conversation_engine import ConversationEngine
from kerkov.core.credentials import get_credential_manager
from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    open_knowledge_base,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.mcp.config import resolve_config_path
from kerkov.core.mcp.manager import McpManager
from kerkov.core.process.session_registry import SessionRegistry
from kerkov.core.process.supervisor import ProcessSupervisor
from kerkov.core.run_workspace import reset_current_workspace, set_current_workspace
from kerkov.core.session import SessionKernel, build_session_state
from kerkov.core.session.approval_mode_manager import (
    APPROVAL_MODE_DESCRIPTIONS,
    ApprovalModeManager,
)
from kerkov.core.session.approval_rules import (
    ApprovalRulesEngine,
    default_approvals_source,
)
from kerkov.core.session.autonomy_manager import (
    AUTONOMY_DESCRIPTIONS,
    AutonomyManager,
    preset_for,
)
from kerkov.core.session.modes import ModeType
from kerkov.core.session.verbosity_manager import (
    VERBOSITY_DESCRIPTIONS,
    VerbosityManager,
)
from kerkov.core.session.verbosity_prompts import (
    VERBOSITY_MARKER,
    VERBOSITY_PROMPTS,
    apply_verbosity_to_system_prompt,
)
from kerkov.core.workflows.compliance import ComplianceRun
from kerkov.core.workflows.registry import COMPLIANCE_WORKFLOW, mode_mismatch_notice
from kerkov.models.basic_models import Message, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.codex.provider import CodexResponseError
from kerkov.tools.base_tool import BaseTool, ToolResult
from kerkov.tools.compliance_profile_tool import ComplianceProfileTool
from kerkov.tools.http_profile_loader import load_http_profiles
from kerkov.tools.ledger_tool import LedgerTool
from kerkov.tools.mcp.mcp_tool import McpServerTool
from kerkov.tools.recall_tool import RecallTool
from kerkov.tools.skill_tool import SkillTool
from kerkov.tools.tool_suite import ToolSuite
from kerkov.web.runtime.agents import WebAgents
from kerkov.web.runtime.approval import WebApprovalController, WebApprovalStrategy
from kerkov.web.runtime.browser import WebBrowser
from kerkov.web.runtime.commands import command_catalog, unsupported_command_detail
from kerkov.web.runtime.compliance import build_run
from kerkov.web.runtime.engagements import WebEngagements
from kerkov.web.runtime.events import WebEventBus, WorkflowEventBus
from kerkov.web.runtime.inventory import WebInventory
from kerkov.web.runtime.mcp import (
    apply_tool_changes,
    expand_prompt_command,
    is_prompt_command,
    run_mcp_command,
)
from kerkov.web.runtime.output import WebOutputSink
from kerkov.web.runtime.projects import ProjectPreparationRequest
from kerkov.web.runtime.providers import WebProviders
from kerkov.web.runtime.scripts import WebScripts

logger = get_logger("WebSession")


def web_command(message: str) -> str | None:
    """Classify commands handled locally rather than by the stoppable engine.

    Delegates to the shared `core.commands` parser so the web and CLI agree on
    what is a command; `clear` (no slash) stays a web-only alias for `/clear`.
    """
    stripped = message.strip()
    if stripped == "clear":
        return "clear"
    parsed = parse(stripped)
    if parsed.intent is Intent.CLEAR:
        return "clear"
    bare = len(parsed.parts) == 1
    if parsed.intent is Intent.MODE:
        return "mode_info" if bare else "mode_change"
    if parsed.intent is Intent.VERBOSITY:
        return "verbosity_info" if bare else "verbosity_change"
    if parsed.intent is Intent.APPROVAL:
        return "approval_info" if bare else "approval_change"
    if parsed.intent is Intent.AUTONOMY:
        return "autonomy_info" if bare else "autonomy_change"
    if parsed.intent is Intent.MCP:
        # A prompt becomes an ordinary, stoppable turn.
        return None if is_prompt_command(stripped) else "mcp"
    if parsed.intent is not Intent.CHAT:
        return "unsupported"
    return None


class WebKerkovSession:
    """Stateful Kerkov runtime owned by one browser WebSocket connection."""

    def __init__(
        self,
        bus: WebEventBus,
        *,
        config: Optional[ConfigManager] = None,
        provider: Optional[BaseLLMProvider] = None,
        process_registry: Optional[SessionRegistry] = None,
        process_supervisor: Optional[ProcessSupervisor] = None,
    ) -> None:
        self.bus = bus
        self.config = config or get_config(reload=True)
        self.credentials = get_credential_manager().new_session()
        self.conversation_history: list[Message] = []
        self._provider_override = provider
        self._closed = False
        self._cleanup: Optional["asyncio.Future[None]"] = None
        self._agent_events_enabled = True
        self._agent_snapshots: dict[str, dict[str, Any]] = {}

        # Shared manager assembly — identical to the CLI's (see
        # core.session.build_session_state). Keep the existing attribute names.
        self.state = build_session_state(self.config, self.credentials)
        self.kernel = SessionKernel(self.state)
        self.provider_manager = self.state.provider_manager
        self.mode_manager = self.state.mode_manager
        self.verbosity_manager = self.state.verbosity_manager
        self.approval_mode_manager = self.state.approval_mode_manager
        self.autonomy_manager = self.state.autonomy_manager
        self.workspace_manager = self.state.workspace_manager
        self.skill_manager = self.state.skill_manager
        self.agent_manager = self.state.agent_manager
        self.agent_registry = self.state.agent_registry
        self.token_tracker = self.state.token_tracker
        self.compactor: Optional[ContextCompactor] = None
        self.compaction_warning_shown = False
        self.workspace_path: Optional[Path] = None
        self.compliance_run: Optional[ComplianceRun] = None
        # Opened when the engagement starts — see `_start_engagement`.
        self.knowledge_base: Optional[EngagementKnowledgeBase] = None
        # Built alongside the knowledge base it wraps (see _open_knowledge_base).
        self.findings_ledger: Optional[FindingsLedger] = None
        self.process_registry = process_registry or SessionRegistry()
        self.process_supervisor = process_supervisor or ProcessSupervisor()
        self.browser = WebBrowser(bus)

        self._startup_warnings: list[str] = []
        try:
            http_profiles = load_http_profiles(
                self.config.http_profiles_path,
                allow_unresolved=True,
            )
        except ValueError as exc:
            http_profiles = {}
            self._startup_warnings.append(
                f"HTTP profiles were not loaded: {exc}. "
                "The http tool will still work for raw URLs."
            )
        self.tools: list[BaseTool] = ToolSuite.get_tools(
            http_profiles,
            process_registry=self.process_registry,
            process_supervisor=self.process_supervisor,
            browser_tool=self.browser.tool,
            ledger_getter=lambda: self.findings_ledger,
            config=self.config,
            credentials=self.credentials,
        )
        self.tools.append(
            SkillTool(
                self.skill_manager,
                credentials=self.credentials,
            )
        )
        if self.config.kb_enabled:
            self.tools.append(RecallTool(lambda: self.knowledge_base))
            self.tools.append(
                LedgerTool(
                    ledger_getter=lambda: self.findings_ledger,
                    engagement_id_getter=lambda: self.engagement_store.engagement_id,
                )
            )
        self._master_tools_snapshot = list(self.tools)
        self._configure_agents()
        # Connections belong to this WebSocket connection, not to an engagement.
        self.mcp: Optional[McpManager] = None
        if self.config.mcp_enabled:
            self.mcp = McpManager(
                resolve_config_path(self.config.mcp_config_path),
                credentials=self.credentials,
                untrusted_names=self.config.untrusted_env_names,
                on_warning=self._warn_mcp,
            )

        user_approvals_path = Path(self.config.user_approvals_path).expanduser()
        project_approvals_source = default_approvals_source(
            start_dir=Path(__file__).resolve()
        )
        rules_engine = ApprovalRulesEngine.from_files(
            user_approvals_path, project_approvals_source
        )
        self.approvals = WebApprovalController(bus)
        self.approval_strategy = WebApprovalStrategy(
            bus=bus,
            controller=self.approvals,
            rules_engine=rules_engine,
            mode_manager=self.approval_mode_manager,
        )
        self.output = WebOutputSink(bus, stream=self.config.stream_output)
        self.engine = ConversationEngine()
        self._lock = asyncio.Lock()
        self.inventory = WebInventory(self)
        self.scripts = WebScripts(self)
        self.providers = WebProviders(self)
        self.agents = WebAgents(self)
        self.engagements = WebEngagements(self)

    @property
    def engagement_store(self) -> EngagementStore:
        # Single source of truth with the kernel: `build_loop_session` reads the
        # store off SessionState, so resume swapping `session.engagement_store`
        # must reach `state` or the turn would write to the pre-resume store.
        return self.state.engagement_store

    @engagement_store.setter
    def engagement_store(self, store: EngagementStore) -> None:
        self.state.engagement_store = store

    def _configure_agents(self) -> None:
        """Give the current engagement its own supervisor and tool handles."""
        self.agent_supervisor = AgentSupervisor(
            self.agent_registry,
            workspace_manager=self.workspace_manager,
            engagement_store=self.engagement_store,
            max_concurrent=self.config.agent_max_concurrent,
        )
        self._agent_listener = partial(self._on_agent_event, self.agent_supervisor)
        self.agent_supervisor.subscribe(self._agent_listener)
        self.tools = list(self._master_tools_snapshot)
        self.tools.append(
            SpawnAgentTool(
                agent_manager=self.agent_manager,
                master_tools=self._master_tools_snapshot,
                provider_getter=lambda: self.provider_manager.provider,
                routing_getter=self.provider_manager.get_agent_routing_policy,
                registry=self.agent_registry,
                supervisor=self.agent_supervisor,
                parent_depth=0,
                max_batch=self.config.agent_max_batch,
                max_iterations=self.config.agent_max_iterations,
                master_engagement_store=self.engagement_store,
                knowledge_base_getter=lambda: self.knowledge_base,
                temperature=self.config.agent_temperature,
                max_response_tokens=self.config.max_response_tokens,
            )
        )
        self.tools.append(AgentWaitTool(self.agent_supervisor))

    def _on_agent_event(
        self, supervisor: AgentSupervisor, agent: AgentSession, _event: str
    ) -> None:
        """Publish only the fields needed by the browser's agent panel."""
        if (
            self._closed
            or not self._agent_events_enabled
            or supervisor is not self.agent_supervisor
        ):
            return
        report = agent.report
        snapshot = {
            "id": agent.id,
            "agent_run_id": agent.agent_run_id,
            "name": agent.display_name(),
            "task": agent.task,
            "profile": agent.profile,
            "state": agent.state.value,
            "cancel_requested": agent.cancel_requested,
            "current_tool": agent.current_tool,
            "iteration": agent.iteration,
            "max_iterations": agent.max_iterations,
            "report": (
                {
                    "status": report.status,
                    "findings": report.findings,
                    "next_steps": report.next_steps,
                    "tokens_used": report.tokens_used,
                }
                if report is not None
                else None
            ),
        }
        if self._agent_snapshots.get(agent.id) == snapshot:
            return
        self._agent_snapshots[agent.id] = snapshot
        self.bus.emit("agent.updated", {"agent": snapshot})

    async def _reset_agents(self) -> None:
        """Settle the old engagement before exposing a fresh agent list."""
        old_supervisor = self.agent_supervisor
        self._agent_events_enabled = False
        old_supervisor.unsubscribe(self._agent_listener)
        try:
            await old_supervisor.cancel_all()
        finally:
            old_supervisor.discard_pending_reports()
        if self._closed:
            return
        self._configure_agents()
        self._agent_snapshots.clear()
        self._agent_events_enabled = True
        self.bus.emit("agents.reset", {})

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self.provider_manager.provider

    async def initialize(self) -> None:
        """Initialize provider state and MCP servers, then notify the browser."""

        for warning in self._startup_warnings:
            self.bus.emit("log.warning", {"message": warning})
        self._startup_warnings.clear()

        # MCP tools must be registered before `session.ready` lists the tools and
        # before the first turn fixes the cached prompt prefix.
        try:
            (provider_ready, error), _ = await asyncio.gather(
                self._initialize_provider(), self.start_mcp()
            )
        except asyncio.CancelledError:
            # The consumer cannot run `disconnect()` until this returns, and the
            # server cancels a disconnected application instead, so a server that
            # already connected would outlive the socket. A task keeps cleanup
            # running even if the server cancels again.
            self._cleanup = asyncio.ensure_future(self.aclose())
            await asyncio.shield(self._cleanup)
            raise
        self._emit_ready(provider_ready=provider_ready, error=error)

    async def _initialize_provider(self) -> tuple[bool, Optional[str]]:
        if self._provider_override is not None:
            self.provider_manager.set_provider(self._provider_override)
            return True, None

        try:
            self.provider_manager.initialize_provider()
            provider = self.provider_manager.provider
            if provider is None:
                return False, f"Unsupported provider: {self.config.llm_provider}"

            valid = await asyncio.to_thread(provider.validate_connection)
            if not valid:
                return False, "Provider connection validation failed."
            return True, None
        except Exception as exc:
            logger.warning(f"Web provider initialization failed: {exc}")
            return False, str(exc)

    async def start_mcp(self) -> None:
        """Connect the configured MCP servers and register their tools."""
        if self.mcp is None:
            return
        ignored = sorted(TRUSTED_SOURCE_SETTINGS & self.config.untrusted_env_names)
        if ignored:
            self.bus.emit(
                "log.warning",
                {
                    "message": f"Ignored {', '.join(ignored)} from "
                    f"{self.config.untrusted_env_file}: a .env outside the Kerkov "
                    "checkout cannot choose MCP servers."
                },
            )
        reserved = {
            tool.name for tool in self.tools if not isinstance(tool, McpServerTool)
        }
        try:
            await self.mcp.start(reserved)
        except Exception:
            # Like the knowledge base, MCP is an enhancement: never stop the console.
            logger.exception("MCP startup failed")
            self.bus.emit(
                "log.warning",
                {"message": "MCP servers could not be started. Check the server log."},
            )
            return
        for warning in self.mcp.warnings():
            self.bus.emit("log.warning", {"message": f"mcp.json: {warning}"})
        if self.mcp.load_error:
            self.bus.emit(
                "log.warning", {"message": f"MCP disabled: {self.mcp.load_error}"}
            )
            return
        self.register_mcp_tools()
        rows = self.mcp.status()
        connected = [row for row in rows if row.state == "connected"]
        if connected:
            self.bus.emit(
                "log.info",
                {
                    "message": f"MCP: {len(connected)} server(s) connected, "
                    f"{len(self.mcp.tools())} tool(s)."
                },
            )
        for row in rows:
            if row.state.startswith(("failed", "invalid")):
                self.bus.emit(
                    "log.warning", {"message": f"MCP server {row.key}: {row.state}"}
                )

    def register_mcp_tools(self) -> None:
        """Swap the advertised MCP tools; changes the cached prompt prefix once."""
        mcp_tools = self.mcp.tools() if self.mcp is not None else []
        # In place: the delegate tool builds future agents and nested delegates
        # from the snapshot. Agents only receive a tool their profile names.
        for tools in (self._master_tools_snapshot, self.tools):
            kept = [tool for tool in tools if not isinstance(tool, McpServerTool)]
            # Ahead of the delegate tools, as `_configure_agents` orders them, so
            # every engagement on this connection advertises the same prefix.
            at = next(
                (
                    index
                    for index, tool in enumerate(kept)
                    if isinstance(tool, (SpawnAgentTool, AgentWaitTool))
                ),
                len(kept),
            )
            tools[:] = kept[:at] + mcp_tools + kept[at:]

    def _warn_mcp(self, text: str) -> None:
        self.bus.emit("log.warning", {"message": f"MCP: {text}"})

    async def handle_user_message(
        self,
        raw_message: str,
        *,
        interrupt: Optional[EventInterrupt] = None,
        workflow_id: Optional[str] = None,
        workflow_request: Optional[dict[str, Any]] = None,
    ) -> None:
        """Handle one browser-submitted message."""

        event_bus = WorkflowEventBus(self.bus, workflow_id) if workflow_id else self.bus
        message = raw_message.strip()
        if not message:
            self.bus.emit("error", {"detail": "Message cannot be empty."})
            return

        if self._lock.locked():
            self.bus.emit(
                "error",
                {"detail": "Kerkov is already working on a message."},
            )
            return

        async with self._lock:
            run = None
            preparing_workflow = workflow_request is not None and bool(workflow_id)
            active_interrupt = interrupt or EventInterrupt()
            try:
                await apply_tool_changes(self)
                if workflow_id is None and is_prompt_command(message):
                    expanded = await run_cancellable(
                        expand_prompt_command(self, message),
                        active_interrupt.interrupt_requested,
                    )
                    if expanded is None:
                        return
                    message = expanded
                elif workflow_id is None and await self._handle_web_command(message):
                    return
                overlay: list[str] = []
                if workflow_request is not None and workflow_id:
                    notice = mode_mismatch_notice(
                        COMPLIANCE_WORKFLOW, self.mode_manager.current_mode.mode_type
                    )
                    if notice:
                        event_bus.emit("log.warning", {"message": notice})
                    run = build_run(self, workflow_request, workflow_id)
                    self.compliance_run = run
                    run.publish()
                    await run.prepare()
                    message = run.plan_message()
                    overlay = [COMPLIANCE_WORKFLOW.overlay]
                    preparing_workflow = False
                await self._run_engine_turn(
                    message,
                    active_interrupt,
                    workflow_bus=event_bus if workflow_id else None,
                    compliance_run=run,
                    transient_prompts=overlay,
                )
            except InterruptedByUser:
                event_bus.emit("log.info", {"message": "Run stopped by user"})
            except CodexResponseError as exc:
                event_bus.emit("provider.error", {"detail": str(exc)})
            except Exception as exc:
                if run is not None:
                    run.failed = True
                    run.record["detail"] = (
                        str(exc)
                        if isinstance(exc, (ValueError, OSError))
                        else "Workflow failed. Review saved evidence and server logs."
                    )
                    if run.record["stage"] == "readiness":
                        run.record["readiness"].append(
                            {
                                "name": "Readiness",
                                "status": "blocked",
                                "detail": run.record["detail"],
                            }
                        )
                expected = preparing_workflow and isinstance(exc, ValueError)
                if expected:
                    detail = (
                        "Invalid compliance settings. Review the selected asset, baseline and connection fields."
                        if isinstance(exc, ValidationError)
                        else str(exc)
                    )
                    logger.info(f"Compliance readiness rejected: {detail}")
                else:
                    logger.exception(f"Web message failed: {exc}")
                    detail = "The request failed. Check the server logs for details."
                event_bus.emit("error", {"detail": detail})
            finally:
                if run is not None:
                    try:
                        await run.finish(active_interrupt)
                    finally:
                        self.compliance_run = None

    async def handle_project_preparation(
        self, request: Any, message_id: str, interrupt: EventInterrupt
    ) -> None:
        event_bus = WorkflowEventBus(self.bus, message_id)
        if self._lock.locked():
            event_bus.emit(
                "error", {"detail": "Kerkov is already working on a message."}
            )
            return

        async with self._lock:
            try:
                if interrupt.interrupt_requested.is_set():
                    return
                project = ProjectPreparationRequest.model_validate(request)
                url = ProfileSource.parse(project.url).url
                if not self.config.execution_enabled:
                    raise ValueError(
                        "Enable EXECUTION_ENABLED before preparing profile projects."
                    )
                tool = next(
                    tool
                    for tool in self.tools
                    if isinstance(tool, ComplianceProfileTool)
                )
                self._apply_system_prompt_if_needed()
                assert self.workspace_path is not None
                token = set_current_workspace(self.workspace_path)
                try:
                    output = WebOutputSink(event_bus, stream=False)
                    params = {
                        "action": "prepare",
                        "url": url,
                        "refresh": project.refresh,
                        "expected_commit": project.commit,
                    }
                    output.tool_start(tool.name, params)
                    approval, result = await self.engine.execute_tool_call(
                        tool,
                        ToolCall(
                            tool_name=tool.name, parameters=params, call_id=message_id
                        ),
                        approval=self.approval_strategy,
                        output=output,
                        interrupt=interrupt,
                        stop_sensitive=True,
                    )
                    if result is None:
                        result = ToolResult(
                            success=False,
                            output="",
                            error=approval.user_message
                            or "Project preparation was denied.",
                        )
                    if result.success:
                        output.tool_succeeded(tool.name, result)
                    else:
                        output.tool_failed(tool.name, result)
                finally:
                    reset_current_workspace(token)
            except InterruptedByUser:
                return
            except ValidationError:
                event_bus.emit(
                    "error", {"detail": "Invalid project preparation request."}
                )
            except ValueError as exc:
                event_bus.emit("error", {"detail": str(exc)})
            except Exception:
                logger.exception("Project preparation failed")
                event_bus.emit(
                    "error",
                    {"detail": "Project preparation failed. Check the server logs."},
                )

    def set_approval_mode(self, value: str) -> None:
        """Switch approval gating; safe mid-turn since gating is read per tool call."""
        approval = ApprovalModeManager.parse(value)
        if approval is None:
            self.bus.emit(
                "error",
                {
                    "detail": "Invalid approval mode. Use strict, normal, "
                    "permissive, or yolo."
                },
            )
            return
        try:
            new_mode = self.approval_mode_manager.set(approval)
        except ValueError as exc:
            self.bus.emit("error", {"detail": str(exc)})
            return
        if self.engagement_store.engagement_id:
            self.engagement_store.update_approval_mode(new_mode.value)
        self.bus.emit(
            "settings.changed",
            {
                "setting": "approval",
                "title": f"Approval mode switched to {new_mode.value}",
                "detail": APPROVAL_MODE_DESCRIPTIONS[new_mode],
                "approval_mode": new_mode.value,
            },
        )

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        user_message: Optional[str] = None,
    ) -> bool:
        return self.approvals.resolve(approval_id, decision, user_message)

    async def handle_inventory_message(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        await self.inventory.handle(content, message_pending=message_pending)

    async def handle_scripts_message(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        await self.scripts.handle(content, message_pending=message_pending)

    async def handle_provider_message(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        await self.providers.handle(content, message_pending=message_pending)

    async def handle_engagement_message(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        await self.engagements.handle(content, message_pending=message_pending)

    async def handle_agent_message(self, content: dict[str, Any]) -> None:
        await self.agents.handle(content)

    async def _retire_engagement(self) -> None:
        """Hide the old inventory immediately, then settle its users and close it."""
        old_kb = self.knowledge_base
        self.knowledge_base = None
        self.findings_ledger = None
        self.workspace_path = None
        self.inventory.reset()
        self.scripts.reset()
        self.browser.reset()
        if self.mcp is not None:
            # Connections persist across engagements; only in-flight calls stop.
            self.mcp.cancel_inflight()
        try:
            await self._reset_agents()
            await self.inventory.settle()
            await self.scripts.settle()
            self.engagements.reset()
            self.engagement_store.detach()
        finally:
            try:
                await self.browser.close(reopen=not self._closed)
            finally:
                if old_kb is not None:
                    old_kb.close()

    def close(self) -> None:
        self._closed = True
        self.providers.codex_login.cancel()
        self.browser.reset(notify=False)
        self.credentials.clear_session()
        self.engagements.release()
        self.agent_supervisor.unsubscribe(self._agent_listener)
        self.approvals.cancel_all()
        self.agent_supervisor.request_cancel_all()
        self.process_supervisor.cancel_all()
        if self.mcp is not None:
            self.mcp.cancel_inflight()
        self._close_knowledge_base()

    async def aclose(self) -> None:
        if not self._closed:
            self.close()
        try:
            await self.agent_supervisor.cancel_all()
        finally:
            # No server process may outlive the connection that started it.
            if self.mcp is not None:
                await self.mcp.close()
            await self.browser.close()

    async def _handle_web_command(self, message: str) -> bool:
        command = web_command(message)
        if command == "clear":
            await self._retire_engagement()
            self.approval_strategy.clear_session_rules()
            self.conversation_history = []
            self.token_tracker.reset()
            self.workspace_path = None
            self.bus.emit("session.cleared", {})
            return True

        if command == "mode_info":
            current = self.mode_manager.current_mode
            self.bus.emit(
                "mode.info",
                {
                    "current": current.mode_type.value,
                    "name": current.name,
                    "description": current.description,
                    "available": [
                        {
                            "id": mode.mode_type.value,
                            "name": mode.name,
                            "description": mode.description,
                        }
                        for mode in self.mode_manager.list_modes().values()
                    ],
                },
            )
            return True

        if command == "mode_change":
            mode_name = message.split(maxsplit=1)[1]
            mode_type = self.mode_manager.parse_mode_string(mode_name)
            if mode_type is None:
                self.bus.emit(
                    "error",
                    {
                        "detail": f"Invalid mode '{mode_name}'. Use red, purple, or blue."
                    },
                )
                return True
            old_mode = self.mode_manager.current_mode
            if mode_type == old_mode.mode_type:
                self.bus.emit(
                    "mode.info",
                    {
                        "current": old_mode.mode_type.value,
                        "name": old_mode.name,
                        "description": old_mode.description,
                    },
                )
                return True
            await self._retire_engagement()
            new_mode = self.mode_manager.switch_mode(mode_type)
            self.conversation_history = []
            self.token_tracker.reset()
            self.approval_strategy.clear_session_rules()
            self.workspace_path = None
            self.bus.emit(
                "mode.changed",
                {
                    "from": old_mode.mode_type.value,
                    "to": new_mode.mode_type.value,
                    "name": new_mode.name,
                    "description": new_mode.description,
                },
            )
            return True

        if command == "mcp":
            await run_mcp_command(self, message)
            return True

        if command is not None and command.split("_")[0] in {
            "verbosity",
            "approval",
            "autonomy",
        }:
            self._handle_settings_command(command, message)
            return True

        if command == "unsupported":
            detail = unsupported_command_detail(message, self.mode_manager)
            self.bus.emit("command.unsupported", {"detail": detail})
            return True

        return False

    def _handle_settings_command(self, command: str, message: str) -> None:
        """Change verbosity/approval/autonomy in place (no engagement teardown).

        Unlike /mode and /clear these keep the conversation; they only adjust how
        the assistant narrates and whether tools need approval, then persist the
        new value to the active engagement so /resume restores it.
        """
        if command == "verbosity_info":
            verbosity_now = self.verbosity_manager.current
            self.bus.emit(
                "settings.info",
                {
                    "setting": "verbosity",
                    "title": f"Verbosity: {verbosity_now.value}",
                    "detail": VERBOSITY_DESCRIPTIONS[verbosity_now],
                },
            )
            return
        if command == "verbosity_change":
            verbosity = VerbosityManager.parse(message.split()[1])
            if verbosity is None:
                self.bus.emit(
                    "error",
                    {
                        "detail": "Invalid verbosity. Use guided, standard, "
                        "aggressive, or silent."
                    },
                )
                return
            new = self.verbosity_manager.set(verbosity)
            apply_verbosity_to_system_prompt(self.conversation_history, new)
            if self.engagement_store.engagement_id:
                self.engagement_store.update_verbosity(new.value)
            self.bus.emit(
                "settings.changed",
                {
                    "setting": "verbosity",
                    "title": f"Verbosity switched to {new.value}",
                    "detail": VERBOSITY_DESCRIPTIONS[new],
                },
            )
            return

        if command == "approval_info":
            approval_now = self.approval_mode_manager.current
            self.bus.emit(
                "settings.info",
                {
                    "setting": "approval",
                    "title": f"Approval mode: {approval_now.value}",
                    "detail": APPROVAL_MODE_DESCRIPTIONS[approval_now],
                },
            )
            return
        if command == "approval_change":
            self.set_approval_mode(message.split()[1])
            return

        if command == "autonomy_info":
            level = self.autonomy_manager.current_level
            if level is None:
                self.bus.emit(
                    "settings.info",
                    {
                        "setting": "autonomy",
                        "title": "Autonomy: custom",
                        "detail": "Verbosity and approval are set independently.",
                    },
                )
                return
            self.bus.emit(
                "settings.info",
                {
                    "setting": "autonomy",
                    "title": f"Autonomy: {level.name.lower()}",
                    "detail": AUTONOMY_DESCRIPTIONS[level],
                },
            )
            return
        if command == "autonomy_change":
            level = AutonomyManager.parse_level(message.split()[1])
            if level is None:
                self.bus.emit(
                    "error",
                    {
                        "detail": "Invalid autonomy level. Use 1-4 or guided, "
                        "standard, aggressive, autonomous."
                    },
                )
                return
            try:
                new_level = self.autonomy_manager.set_level(level.value)
            except ValueError as exc:
                self.bus.emit("error", {"detail": str(exc)})
                return
            new_verbosity, new_approval = preset_for(new_level)
            apply_verbosity_to_system_prompt(self.conversation_history, new_verbosity)
            if self.engagement_store.engagement_id:
                self.engagement_store.update_verbosity(new_verbosity.value)
                self.engagement_store.update_approval_mode(new_approval.value)
            self.bus.emit(
                "settings.changed",
                {
                    "setting": "autonomy",
                    "title": f"Autonomy switched to {new_level.name.lower()}",
                    "detail": AUTONOMY_DESCRIPTIONS[new_level],
                    "approval_mode": new_approval.value,
                },
            )

    async def _run_engine_turn(
        self,
        user_message: str,
        interrupt: EventInterrupt,
        workflow_bus: Optional[WebEventBus] = None,
        compliance_run: Optional[ComplianceRun] = None,
        transient_prompts: Sequence[str] = (),
    ) -> None:
        if not self.provider:
            (workflow_bus or self.bus).emit(
                "provider.error",
                {
                    "detail": (
                        "Choose a configured provider using the model selector. "
                        "Use Manage providers to add credentials or a local endpoint."
                    )
                },
            )
            return

        self._apply_system_prompt_if_needed()
        self._deliver_agent_reports()
        self._append_message(
            Message(
                role="user", content=user_message, context_only=workflow_bus is not None
            )
        )
        if interrupt.interrupt_requested.is_set():
            return
        await run_cancellable(
            self._ensure_compactor(interrupt), interrupt.interrupt_requested
        )

        token = (
            set_current_workspace(self.workspace_path) if self.workspace_path else None
        )
        try:
            session = self.kernel.build_loop_session(
                history=self.conversation_history,
                tools=self.tools,
                approval=compliance_run or self.approval_strategy,
                output=(
                    compliance_run.output
                    if compliance_run
                    else (
                        WebOutputSink(workflow_bus, stream=False)
                        if workflow_bus
                        else self.output
                    )
                ),
                knowledge_base=self.knowledge_base,
                ledger=self.findings_ledger,
                compactor=self.compactor,
                interrupt=interrupt,
                interrupt_ends_turn=True,
                transient_prompts=transient_prompts,
                compaction_warning_shown=self.compaction_warning_shown,
                context_only=workflow_bus is not None,
            )
            await self.engine.run_loop(session)
            self.compaction_warning_shown = session.compaction_warning_shown
        finally:
            if token is not None:
                reset_current_workspace(token)

    def engagement_workspace(self) -> Optional[Path]:
        """The run workspace, starting the engagement if a turn can follow."""
        if self.provider:
            self._apply_system_prompt_if_needed()
        return self.workspace_path

    def _apply_system_prompt_if_needed(self) -> None:
        if self.conversation_history:
            return

        system_prompt = self.mode_manager.current_mode.system_prompt
        skill_index = self.skill_manager.get_catalog_summary()
        if skill_index:
            system_prompt += "\n\n" + skill_index

        agent_index = self.agent_manager.get_index_summary()
        if agent_index:
            system_prompt += "\n\n" + agent_index

        system_prompt += (
            VERBOSITY_MARKER + VERBOSITY_PROMPTS[self.verbosity_manager.current]
        )

        model = self.provider.model if self.provider else "unknown"
        engagement_id = self.engagement_store.start_engagement(
            mode=self.mode_manager.current_mode.name,
            model=model,
            verbosity=self.verbosity_manager.current.value,
            approval_mode=self.approval_mode_manager.current.value,
        )
        self.engagements.claim(engagement_id)
        self.workspace_path = self.workspace_manager.activate(engagement_id)
        self.approval_strategy.set_workspace(self.workspace_path)
        self._open_knowledge_base(engagement_id)
        self.inventory.engagement_id = engagement_id
        self.scripts.engagement_id = engagement_id
        self.bus.emit(
            "workspace.ready",
            {
                "engagement_id": engagement_id,
                "path": str(self.workspace_path),
            },
        )
        self._append_message(Message(role="system", content=system_prompt))

    def _open_knowledge_base(self, engagement_id: str) -> None:
        """Open the store for a starting engagement, closing any previous one.

        `/clear` starts a fresh engagement on the same session, so this runs
        more than once per connection; without the close, each new engagement
        strands the previous SQLite connection.
        """
        self._close_knowledge_base()
        if not self.config.kb_enabled:
            return
        try:
            self.knowledge_base = open_knowledge_base(
                engagement_id, self.engagement_store.store_dir
            )
            self.findings_ledger = FindingsLedger(self.knowledge_base)
        except Exception as exc:
            # Recall is an enhancement; a broken store must not stop the console.
            logger.warning(f"Could not open knowledge base for {engagement_id}: {exc}")
            self.knowledge_base = None
            self.findings_ledger = None

    def _close_knowledge_base(self) -> None:
        if self.knowledge_base is not None:
            self.knowledge_base.close()
            self.knowledge_base = None
        self.findings_ledger = None

    def _append_message(self, message: Message) -> None:
        self.conversation_history.append(message)
        self.engagement_store.append_message(message)

    def _deliver_agent_reports(self) -> None:
        """Hand the model any detached agent report it has not seen, once each."""
        for session, report in self.agent_supervisor.drain_finished_reports():
            self._append_message(
                Message(
                    role="user",
                    content=format_report_for_injection(session, report),
                )
            )

    async def _ensure_compactor(
        self, interrupt: Optional[EventInterrupt] = None
    ) -> None:
        if interrupt is not None and interrupt.interrupt_requested.is_set():
            raise InterruptedByUser()
        provider = self.provider
        if not provider:
            self.compactor = None
            return

        max_ctx = (
            await provider.get_max_context_tokens() or self.config.max_context_tokens
        )
        if interrupt is not None and interrupt.interrupt_requested.is_set():
            raise InterruptedByUser()
        self._configure_compactor(max_ctx)

    def _configure_compactor(self, max_ctx: int) -> None:
        """Apply resolved model limits without changing conversation accounting."""
        ledger_active = self.findings_ledger is not None
        if self.compactor is None:
            self.compactor = ContextCompactor(
                token_tracker=self.token_tracker,
                max_context_tokens=max_ctx,
                threshold_pct=self.config.compaction_threshold_pct,
                summary_reserve_tokens=self.config.summary_reserve_tokens,
                ledger_active=ledger_active,
            )
        else:
            self.compactor.update_limits(max_ctx)
            self.compactor.ledger_active = ledger_active
        self.compaction_warning_shown = False

    def _emit_ready(
        self,
        *,
        provider_ready: bool,
        error: Optional[str] = None,
    ) -> None:
        provider = self.provider
        current_mode = self.mode_manager.current_mode
        self.bus.emit(
            "session.ready",
            {
                "title": "Kerkov web console online",
                "detail": "Django + Channels are connected to the Kerkov runtime.",
                "mode": current_mode.mode_type.value,
                "mode_name": current_mode.name,
                "provider_ready": provider_ready,
                "provider": provider.get_model_info() if provider else None,
                "error": error,
                "tools": [tool.name for tool in self.tools],
                "approval_mode": self.approval_mode_manager.current.value,
                "verbosity": self.verbosity_manager.current.value,
                "commands": command_catalog(self.mode_manager),
            },
        )
