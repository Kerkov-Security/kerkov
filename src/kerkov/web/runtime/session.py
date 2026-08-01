"""Web session runtime for connecting Django to Kerkov's core engine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.cli.approval_mode_manager import ApprovalMode, ApprovalModeManager
from kerkov.cli.approval_rules import ApprovalRulesEngine, default_approvals_source
from kerkov.cli.autonomy_manager import AutonomyLevel, preset_for
from kerkov.cli.engagement_store import EngagementStore
from kerkov.cli.mode_manager import ModeManager
from kerkov.cli.modes import ModeType
from kerkov.cli.verbosity_manager import Verbosity, VerbosityManager
from kerkov.cli.verbosity_prompts import VERBOSITY_MARKER, VERBOSITY_PROMPTS
from kerkov.context.context_compactor import ContextCompactor
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.config_manager import ConfigManager, get_config
from kerkov.core.conversation_engine import ConversationEngine, LoopSession
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.process.session_registry import SessionRegistry
from kerkov.core.process.supervisor import ProcessSupervisor
from kerkov.core.run_workspace import (
    RunWorkspaceManager,
    reset_current_workspace,
    set_current_workspace,
)
from kerkov.models.basic_models import Message
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.providers.provider_manager import ProviderManager
from kerkov.tools.base_tool import BaseTool
from kerkov.tools.http_profile_loader import load_http_profiles
from kerkov.tools.skill_tool import SkillTool
from kerkov.tools.tool_suite import ToolSuite
from kerkov.web.runtime.approval import WebApprovalController, WebApprovalStrategy
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.output import WebOutputSink

logger = get_logger("WebSession")


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
        self.conversation_history: list[Message] = []
        self.provider_manager = ProviderManager(self.config)
        self._provider_override = provider

        self.mode_manager = ModeManager()
        preset_verbosity, preset_approval = preset_for(
            AutonomyLevel(self.config.autonomy_level)
        )
        self.verbosity_manager = VerbosityManager(
            initial=(
                Verbosity(self.config.verbosity)
                if self.config.verbosity
                else preset_verbosity
            )
        )
        self.approval_mode_manager = ApprovalModeManager(
            initial=(
                ApprovalMode(self.config.approval_mode)
                if self.config.approval_mode
                else preset_approval
            ),
            allow_autonomous=self.config.allow_autonomous,
        )

        self.workspace_manager = RunWorkspaceManager(self.config.workspace_dir)
        self.skill_manager = SkillManager()
        self.agent_manager = AgentManager()
        self.agent_registry = AgentRegistry(
            max_depth=self.config.agent_max_depth,
            max_total=self.config.agent_max_total,
        )
        self.engagement_store = EngagementStore()
        self.token_tracker = TokenTracker()
        self.compactor: Optional[ContextCompactor] = None
        self.compaction_warning_shown = False
        self.workspace_path: Optional[Path] = None
        self.process_registry = process_registry or SessionRegistry()
        self.process_supervisor = process_supervisor or ProcessSupervisor()

        self._startup_warnings: list[str] = []
        try:
            http_profiles = load_http_profiles(self.config.http_profiles_path)
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
            include_browser=False,
        )
        self.tools.append(SkillTool(self.skill_manager))
        master_tools_snapshot = list(self.tools)
        self.tools.append(
            SpawnAgentTool(
                agent_manager=self.agent_manager,
                master_tools=master_tools_snapshot,
                provider_getter=lambda: self.provider_manager.provider,
                registry=self.agent_registry,
                parent_depth=0,
                max_batch=self.config.agent_max_batch,
                max_iterations=self.config.agent_max_iterations,
                master_engagement_store=self.engagement_store,
                temperature=self.config.agent_temperature,
                max_response_tokens=self.config.max_response_tokens,
            )
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
        self.output = WebOutputSink(bus)
        self.engine = ConversationEngine()
        self._lock = asyncio.Lock()

    @property
    def provider(self) -> Optional[BaseLLMProvider]:
        return self.provider_manager.provider

    async def initialize(self) -> None:
        """Initialize provider state and notify the browser."""

        for warning in self._startup_warnings:
            self.bus.emit("log.warning", {"message": warning})
        self._startup_warnings.clear()

        if self._provider_override is not None:
            self.provider_manager.set_provider(self._provider_override)
            self._emit_ready(provider_ready=True)
            return

        try:
            self.provider_manager.initialize_provider()
            provider = self.provider_manager.provider
            if provider is None:
                self._emit_ready(
                    provider_ready=False,
                    error=f"Unsupported provider: {self.config.llm_provider}",
                )
                return

            valid = await asyncio.to_thread(provider.validate_connection)
            if not valid:
                self._emit_ready(
                    provider_ready=False,
                    error="Provider connection validation failed.",
                )
                return

            self._emit_ready(provider_ready=True)
        except Exception as exc:
            logger.warning(f"Web provider initialization failed: {exc}")
            self._emit_ready(provider_ready=False, error=str(exc))

    async def handle_user_message(self, raw_message: str) -> None:
        """Handle one browser-submitted message."""

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
            try:
                if await self._handle_web_command(message):
                    return
                await self._run_engine_turn(message)
            except Exception as exc:
                logger.exception(f"Web message failed: {exc}")
                self.bus.emit(
                    "error",
                    {
                        "detail": "The request failed. Check the server logs for details."
                    },
                )

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        user_message: Optional[str] = None,
    ) -> bool:
        return self.approvals.resolve(approval_id, decision, user_message)

    def close(self) -> None:
        self.approvals.cancel_all()
        self.process_supervisor.cancel_all()

    async def _handle_web_command(self, message: str) -> bool:
        if message == "/clear" or message == "clear":
            self.approval_strategy.clear_session_rules()
            self.conversation_history = []
            self.token_tracker.reset()
            self.workspace_path = None
            self.bus.emit("session.cleared", {})
            return True

        if message == "/mode":
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

        if message.startswith("/mode "):
            mode_name = message.split(maxsplit=1)[1]
            mode_type = self.mode_manager.parse_mode_string(mode_name)
            if mode_type is None:
                self.bus.emit(
                    "error",
                    {
                        "detail": f"Invalid mode '{mode_name}'. Use recon, audit, offensive, or ctf."
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

        return False

    async def _run_engine_turn(self, user_message: str) -> None:
        if not self.provider:
            self.bus.emit(
                "provider.error",
                {
                    "detail": (
                        "No provider is currently initialized. Configure .env or "
                        "Codex auth, then restart kerkov-web."
                    )
                },
            )
            return

        self._apply_system_prompt_if_needed()
        self._append_message(Message(role="user", content=user_message))
        await self._ensure_compactor()

        token = (
            set_current_workspace(self.workspace_path) if self.workspace_path else None
        )
        try:
            session = LoopSession(
                provider=self.provider,
                history=self.conversation_history,
                tools=self.tools,
                approval=self.approval_strategy,
                output=self.output,
                max_iterations=100,
                temperature=self.config.agent_temperature,
                max_response_tokens=self.config.max_response_tokens,
                engagement_store=self.engagement_store,
                compactor=self.compactor,
                token_tracker=self.token_tracker,
                compaction_warning_shown=self.compaction_warning_shown,
            )
            await self.engine.run_loop(session)
            self.compaction_warning_shown = session.compaction_warning_shown
        finally:
            if token is not None:
                reset_current_workspace(token)

    def _apply_system_prompt_if_needed(self) -> None:
        if self.conversation_history:
            return

        system_prompt = self.mode_manager.current_mode.system_prompt
        skill_index = self.skill_manager.get_index_summary()
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
        self.workspace_path = self.workspace_manager.activate(engagement_id)
        self.approval_strategy.set_workspace(self.workspace_path)
        self.bus.emit(
            "workspace.ready",
            {
                "engagement_id": engagement_id,
                "path": str(self.workspace_path),
            },
        )
        self._append_message(Message(role="system", content=system_prompt))

    def _append_message(self, message: Message) -> None:
        self.conversation_history.append(message)
        self.engagement_store.append_message(message)

    async def _ensure_compactor(self) -> None:
        provider = self.provider
        if not provider:
            self.compactor = None
            return

        max_ctx = (
            await provider.get_max_context_tokens() or self.config.max_context_tokens
        )
        if self.compactor is None:
            self.compactor = ContextCompactor(
                token_tracker=self.token_tracker,
                max_context_tokens=max_ctx,
                threshold_pct=self.config.compaction_threshold_pct,
                summary_reserve_tokens=self.config.summary_reserve_tokens,
            )
        else:
            self.compactor.update_limits(max_ctx)
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
            },
        )
