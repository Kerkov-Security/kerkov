#!/usr/bin/env python3
"""
Kerkov - Security-focused conversational AI CLI
Interactive CLI for Kerkov
"""

import asyncio
import sys
from html import escape as escape_html
from pathlib import Path
from typing import List, Optional

from prompt_toolkit.formatted_text import AnyFormattedText
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_supervisor import AgentSupervisor
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.agents.agent_wait_tool import AgentWaitTool
from kerkov.agents.report_format import format_report_for_injection
from kerkov.cli.agent_attach import AgentAttachView
from kerkov.cli.agent_display import agent_prompt_label, agent_strip
from kerkov.cli.agent_focus_manager import AgentFocusManager
from kerkov.cli.agent_review import AttachedAgentReview
from kerkov.cli.approval_mode_manager import ApprovalMode, ApprovalModeManager
from kerkov.cli.approval_rules import ApprovalRulesEngine, default_approvals_source
from kerkov.cli.autonomy_manager import AutonomyLevel, AutonomyManager, preset_for
from kerkov.cli.buffering_sink import BufferingSink
from kerkov.cli.engagement_restore import restored_setting
from kerkov.cli.engagement_store import EngagementStore, Engangement
from kerkov.cli.input_manager import SWITCHER_SENTINEL, InputManager
from kerkov.cli.input_parser import GeneralMessage, InputParser
from kerkov.cli.messages.agents_message import AgentsMessage
from kerkov.cli.messages.user_message import UserMessage
from kerkov.cli.mode_manager import ModeManager
from kerkov.cli.modes import ModeType
from kerkov.cli.provider_setup_extension import (
    DEFAULT_OLLAMA_BASE_URL,
    PROVIDER_DISPLAY_NAMES,
    SETUP_LATER_PROVIDER,
    ProviderSetupExtension,
    _normalize_ollama_base_url,
)
from kerkov.cli.tool_approval_helper import ToolApprovalCliHelper
from kerkov.cli.verbosity_manager import Verbosity, VerbosityManager
from kerkov.cli.verbosity_prompts import (
    VERBOSITY_MARKER,
    VERBOSITY_PROMPTS,
    apply_verbosity_to_system_prompt,
)
from kerkov.context.context_compactor import ContextCompactor
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.config_manager import get_config
from kerkov.core.conversation_engine import (
    ConsoleSink,
    ConversationEngine,
    InteractiveApproval,
    LoopSession,
)
from kerkov.core.credentials import CredentialManager, get_credential_manager
from kerkov.core.knowledge_base.embeddings import resolve_embedding_backend
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    open_knowledge_base,
    resolve_root_engagement_id,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.run_workspace import RunWorkspaceManager, get_current_workspace
from kerkov.models.basic_models import Message
from kerkov.providers.provider_manager import ProviderManager
from kerkov.tools.base_tool import BaseTool
from kerkov.tools.http_profile_loader import load_http_profiles
from kerkov.tools.ledger_tool import LedgerTool
from kerkov.tools.recall_tool import RecallTool
from kerkov.tools.skill_tool import SkillTool
from kerkov.tools.tool_suite import ToolSuite

console = Console()

logger = get_logger("KerkovCLI")

NON_INTERACTIVE_EXECUTION_PROMPT = """
NON-INTERACTIVE EXECUTION MODE:
- This invocation is one-shot and may not have a follow-up turn.
- If the user's request can be satisfied with available tools, execute the needed tool calls in this same response.
- Do not stop after only describing a plan or saying you will do something later.
- Prefer action over narration for direct requests like "run", "scan", "check", "execute", "do it", or "proceed".
- If a task is genuinely unsafe or requires clarification, say so briefly instead of pretending work was done.
""".strip()


class KerkovCLI(ProviderSetupExtension):
    """Interactive CLI for Kerkov."""

    def __init__(self, mode: Optional[ModeType] = None):
        """
        Initialize the CLI.

        Args:
            mode: Initial operational mode (defaults to AUDIT)
        """
        self.config = get_config()
        self.credentials: CredentialManager = get_credential_manager()
        self.conversation_history: List[Message] = []
        self.provder_manager = ProviderManager(self.config, self.credentials)
        self.mode_manager = ModeManager(initial_mode=mode)
        self.console = console
        self.output_sink = BufferingSink(
            ConsoleSink(console, stream=self.config.stream_output)
        )

        # Derive verbosity + approval_mode from the autonomy preset, then
        # override with the explicit axis env vars if set.
        preset_verbosity, preset_approval = preset_for(
            AutonomyLevel(self.config.autonomy_level)
        )
        initial_verbosity = (
            Verbosity(self.config.verbosity)
            if self.config.verbosity
            else preset_verbosity
        )
        initial_approval = (
            ApprovalMode(self.config.approval_mode)
            if self.config.approval_mode
            else preset_approval
        )

        self.verbosity_manager = VerbosityManager(initial=initial_verbosity)
        self.approval_mode_manager = ApprovalModeManager(
            initial=initial_approval,
            allow_autonomous=self.config.allow_autonomous,
        )
        self.autonomy_manager = AutonomyManager(
            initial_level=self.config.autonomy_level,
            allow_autonomous=self.config.allow_autonomous,
            verbosity_manager=self.verbosity_manager,
            approval_mode_manager=self.approval_mode_manager,
        )
        self.input_manager = InputManager(console)
        user_approvals_path = Path(self.config.user_approvals_path).expanduser()
        project_approvals_source = default_approvals_source(
            start_dir=Path(__file__).resolve().parent
        )
        rules_engine = ApprovalRulesEngine.from_files(
            user_approvals_path, project_approvals_source
        )
        self.tool_approval_helper = ToolApprovalCliHelper(
            self.input_manager, rules_engine
        )
        self.workspace_manager = RunWorkspaceManager(self.config.workspace_dir)
        self.skill_manager = SkillManager()
        self.agent_manager = AgentManager()
        self.agent_registry = AgentRegistry(
            max_depth=self.config.agent_max_depth,
            max_total=self.config.agent_max_total,
        )
        http_profiles = load_http_profiles(
            self.config.http_profiles_path,
            allow_unresolved=True,
        )
        self.tools: List[BaseTool] = ToolSuite.get_tools(
            http_profiles,
            config=self.config,
            credentials=self.credentials,
            credential_prompt=self.prompt_for_credential,
        )
        self.tools.append(
            SkillTool(
                self.skill_manager,
                credentials=self.credentials,
                credential_prompt=self.prompt_for_credential,
            )
        )
        self.engagement_store = EngagementStore()
        self._knowledge_base: Optional[EngagementKnowledgeBase] = None
        self._findings_ledger: Optional[FindingsLedger] = None
        if self.config.kb_enabled:
            self.tools.append(RecallTool(lambda: self._knowledge_base))
            self.tools.append(
                LedgerTool(
                    ledger_getter=lambda: self._findings_ledger,
                    engagement_id_getter=lambda: self.engagement_store.engagement_id,
                )
            )
        # Shared master tool pool — agents draw their tool set from here. The
        # spawn tool itself is appended afterward and intentionally not in
        # the pool; agents receive a SpawnAgentTool reconstructed at their
        # depth (see `agent_tool.py`). Keep this list so reloads also update
        # the tools available to future agents, including nested delegates.
        self._master_tools = list(self.tools)
        self.agent_focus = AgentFocusManager()
        self.agent_review = AttachedAgentReview(
            self.agent_focus,
            self.input_manager,
            self.tool_approval_helper,
            self.approval_mode_manager,
            self.output_sink,
        )
        self.agent_supervisor = AgentSupervisor(
            self.agent_registry,
            workspace_manager=self.workspace_manager,
            engagement_store=self.engagement_store,
            max_concurrent=self.config.agent_max_concurrent,
        )
        self.tools.append(
            SpawnAgentTool(
                agent_manager=self.agent_manager,
                master_tools=self._master_tools,
                provider_getter=lambda: self.provder_manager.provider,
                registry=self.agent_registry,
                supervisor=self.agent_supervisor,
                parent_depth=0,
                max_batch=self.config.agent_max_batch,
                max_iterations=self.config.agent_max_iterations,
                master_engagement_store=self.engagement_store,
                knowledge_base_getter=lambda: self._knowledge_base,
                temperature=self.config.agent_temperature,
                max_response_tokens=self.config.max_response_tokens,
                review_gate=self.agent_review,
            )
        )
        self.tools.append(AgentWaitTool(self.agent_supervisor))
        self.agent_attach = AgentAttachView(
            console, self.agent_supervisor, self.agent_focus, self.output_sink
        )
        self.input_manager.set_status_provider(self._agent_status_strip)
        self.token_tracker = TokenTracker()
        self.compactor: Optional[ContextCompactor] = None
        self._compaction_warning_shown = False
        self._transient_system_prompts: List[str] = []
        self._switcher_task: Optional["asyncio.Task[None]"] = None
        self._switcher_handoff: Optional["asyncio.Task[None]"] = None
        self.engine = ConversationEngine()

    def reload_tools(self) -> None:
        """Refresh search registration without resetting stateful session tools."""
        search_tools = ToolSuite.get_search_tools(
            config=get_config(reload=True),
            credentials=self.credentials,
            credential_prompt=self.prompt_for_credential,
        )
        # Mutate the shared lists so existing delegates see the updated pool
        # when building new agents. Running agents keep their current tools.
        for tools in (self.tools, self._master_tools):
            tools[:] = [
                tool for tool in tools if tool.name != "web_search"
            ] + search_tools

    async def _reconfigure_context_for_provider(self) -> None:
        """Re-sync the context window to the active provider/model.

        Called after provider or model switches so the compactor targets the
        new window. The local token estimator is provider-agnostic, so we
        preserve tracked token history to avoid losing mid-conversation state.
        """
        await self._ensure_compactor()

    async def _ensure_compactor(self) -> None:
        """Build (or refresh) the compactor for the current provider/model."""
        provider = self.provder_manager.provider
        if not provider:
            self.compactor = None
            return

        max_ctx = (
            await provider.get_max_context_tokens() or self.config.max_context_tokens
        )
        # Keyed on the live ledger, not on `kb_enabled`: opening the store can
        # fail, and telling the summarizer that facts are preserved elsewhere
        # when they are not lets it discard the only copy.
        ledger_active = self._findings_ledger is not None
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
        self._compaction_warning_shown = False

    def _append_message_to_conversation(self, msg: Message) -> None:
        """Append a message to history and engagement store."""
        self.conversation_history.append(msg)
        self.engagement_store.append_message(msg)

    def _deliver_agent_reports(self) -> int:
        """Hand the master any agent report it has not seen, exactly once.

        The mechanical half of the detached contract: `agent_wait` is how the
        model asks for a report, this is how it gets one it never asked for.
        Only the report crosses over — never the agent's chatter (see #147).
        """
        delivered = self.agent_supervisor.drain_finished_reports()
        for session, report in delivered:
            self._append_message_to_conversation(
                Message(
                    role="user",
                    content=format_report_for_injection(session, report),
                )
            )
            self.output_sink.info(
                f"{escape(session.display_name())} finished "
                f"({report.status}) — report added to the conversation."
            )
        return len(delivered)

    async def shutdown_agents(self) -> None:
        """Cancel every outstanding agent so nothing is orphaned on exit."""
        cancelled = await self.agent_supervisor.cancel_all()
        if not cancelled:
            return
        names = [
            escape(s.display_name())
            for s in (self.agent_supervisor.get(i) for i in cancelled)
            if s is not None
        ]
        console.print(
            f"[dim]Cancelled {len(cancelled)} running agent(s): "
            f"{', '.join(names)}[/dim]"
        )
        # Only the interrupted runs are worth a resume hint — a finished agent
        # already reported into the conversation being resumed anyway.
        for agent_id in cancelled:
            session = self.agent_supervisor.get(agent_id)
            if session is not None and session.engagement_id:
                console.print(
                    f"[dim]  {escape(session.display_name())} transcript: "
                    f"uv run kerkov --resume {session.engagement_id}[/dim]"
                )

    async def settle_agents_for_conversation_switch(self) -> None:
        """Close out outstanding agents before the conversation underneath changes.

        An agent's report is addressed to the engagement that spawned it, and
        `_deliver_agent_reports` runs at the next turn regardless of which
        conversation that now is — so a `/clear` or `/resume` mid-flight would
        file (and persist) one engagement's findings under another.
        """
        self.agent_attach.detach()
        await self.shutdown_agents()
        discarded = self.agent_supervisor.discard_pending_reports()
        if discarded:
            console.print(
                f"[dim]Discarded {discarded} undelivered agent report(s) from "
                "the previous conversation.[/dim]"
            )

    async def run(self) -> None:
        """Run the interactive CLI loop."""
        self._display_welcome()
        if self.provder_manager.provider is None:
            selected_provider = await self._select_startup_provider()
            activated = (
                await self.activate_provider_interactive(
                    selected_provider,
                    validate_existing=True,
                )
                if selected_provider is not None
                else False
            )
            if not activated:
                self._display_provider_setup_guidance()

        while True:
            try:
                user_input = await self.input_manager.get_line(self._prompt_html())
                if user_input == SWITCHER_SENTINEL:
                    await self._execute_user_message_safely(AgentsMessage())
                    continue

                user_message = InputParser.parse_for_message_type(user_input)

                logger.info(f"User message parsed: type:{type(user_message)}")
                if isinstance(user_message, GeneralMessage):
                    logger.info(f"User typed: {user_message.user_input}")
                    if self._route_input_to_focused_agent(user_message):
                        continue

                await self._execute_user_message_safely(user_message)
            except KeyboardInterrupt:
                console.print("\n[dim]Exiting due to keyboard interruption[/dim]")
                await self.shutdown_agents()
                self.print_resume_command()
                break
            except EOFError:
                console.print("\n[dim]Exiting due to end of file error[/dim]")
                await self.shutdown_agents()
                self.print_resume_command()
                break

    def _prompt_html(self) -> str:
        """The prompt prefix: mode-coloured on the master, agent-named when attached."""
        agent_id = self.agent_focus.agent_id
        if agent_id is not None:
            session = self.agent_supervisor.get(agent_id)
            if session is None:
                # A focus that no longer resolves would strand the prompt on an
                # agent nothing can detach from.
                self.agent_focus.focus_master()
            else:
                label = escape_html(agent_prompt_label(session))
                return f'<style fg="cyan">{label}</style> <b>›</b> '

        mode = self.mode_manager.current_mode
        return (
            f'<style fg="{mode.color}">{mode.name[0]}</style>'
            f' <b><style fg="cyan">You:</style></b> '
        )

    def _agent_status_strip(self) -> Optional[AnyFormattedText]:
        """The strip under the prompt: focus, plus what is running behind it."""
        sessions = [
            s
            for s in self.agent_supervisor.list()
            if s.is_active or not s.report_delivered
        ]
        if not any(s.is_active for s in sessions):
            return None
        sessions.sort(key=lambda s: not s.is_active)
        return agent_strip(sessions, focused=self.agent_focus.current)

    def _route_input_to_focused_agent(self, message: GeneralMessage) -> bool:
        """Queue chat typed at an attached agent; returns True when handled.

        Slash commands never reach here, so `/back` and `/q` keep working while
        attached. Sending the text to the master instead would be worse than
        dropping it: the user is looking at the agent's output and is
        addressing that.
        """
        text = message.user_input.strip()
        if self.agent_focus.is_master or not text:
            return False

        session = self.agent_supervisor.get(self.agent_focus.agent_id or "")
        if session is None:
            console.print(
                "[yellow]That agent is no longer available.[/yellow] "
                "[dim]Back on the main conversation.[/dim]"
            )
            console.print()
            self.agent_focus.focus_master()
            return True

        name = escape(session.display_name())
        if session.is_finished:
            console.print(
                f"[yellow]{name} has finished — input goes nowhere.[/yellow] "
                "[dim]`/back` returns to the main conversation.[/dim]"
            )
            console.print()
            return True

        session.inbox.put_nowait(text)
        # Through the agent's own sink, not the console: the line belongs in
        # its buffer so it shows up inline now and again on a re-attach.
        session.events.info(f"> {text}")
        console.print(
            f"[dim]Queued for {name} — it reads this at its next loop "
            "boundary, and may finish first.[/dim]"
        )
        console.print()
        return True

    async def run_non_interactive(
        self, inputs: List[str], *, show_welcome: bool = False
    ) -> None:
        """Process one or more inputs without starting the prompt loop."""
        if show_welcome:
            self._display_welcome()

        self.tool_approval_helper.non_interactive_auto_approve = True
        self._transient_system_prompts.append(NON_INTERACTIVE_EXECUTION_PROMPT)
        try:
            for raw_input in inputs:
                user_message = InputParser.parse_for_message_type(raw_input)
                await self._execute_user_message_safely(user_message)
            await self._settle_agents_non_interactive()
        finally:
            self.tool_approval_helper.non_interactive_auto_approve = False
            if self._transient_system_prompts:
                self._transient_system_prompts.pop()

    async def _settle_agents_non_interactive(self) -> None:
        """Let detached agents finish before a one-shot invocation exits.

        There is no next turn to deliver into, so the reports are recorded in
        the transcript (a `--continue` picks them up) and printed. A one-shot
        run that just exits would strand the work it paid for.
        """
        outstanding = self.agent_supervisor.active()
        if not outstanding:
            self._deliver_agent_reports()
            return

        console.print(
            f"[dim]Waiting for {len(outstanding)} background agent(s) to "
            f"finish: {', '.join(escape(s.display_name()) for s in outstanding)}[/dim]"
        )
        outcome = await self.agent_supervisor.wait()
        for session, report in outcome.reports:
            content = format_report_for_injection(session, report)
            self._append_message_to_conversation(Message(role="user", content=content))
            console.print(Markdown(content))
        await self.agent_supervisor.cancel_all()

    async def _execute_user_message_safely(self, user_message: UserMessage) -> None:
        """Keep an individual command failure from terminating the CLI."""

        try:
            await user_message.execute(self)
        except Exception as exc:
            error_type = type(exc).__name__
            logger.error(
                "Unhandled CLI message failure (type=%s, message_type=%s)",
                error_type,
                type(user_message).__name__,
            )
            self.console.print(
                f"[bold red]Command failed ({error_type}).[/bold red] "
                "Kerkov is still running."
            )
            self.console.print(
                "[dim]Try `/help`, or choose a provider with "
                "`/provider <name>` if setup is incomplete.[/dim]"
            )
            self.console.print()

    def _display_welcome(self) -> None:
        """Display welcome message."""
        current_mode = self.mode_manager.current_mode
        current_autonomy = self.autonomy_manager.current_level
        welcome_text = """
# Kerkov

Security-focused conversational AI assistant for penetration testing and security analysis.

**Mode:** {mode_name} ({mode_desc}) · **Autonomy:** {autonomy_label}
**Verbosity:** {verbosity} · **Approval:** {approval_mode}
**Model:** {model} · **Workspace:** {workspace}

Type `/help` for commands · `/q` to exit
        """.format(
            mode_name=current_mode.name,
            mode_desc=current_mode.description,
            autonomy_label=(
                f"Level {current_autonomy.value} ({current_autonomy.name.capitalize()})"
                if current_autonomy is not None
                else "Custom (verbosity and approval set independently)"
            ),
            verbosity=self.verbosity_manager.current.value.capitalize(),
            approval_mode=self.approval_mode_manager.current.value.capitalize(),
            workspace=(
                str(get_current_workspace())
                if get_current_workspace()
                else "created after first message"
            ),
            model=(
                self.provder_manager.provider.model
                if self.provder_manager.provider
                else "N/A"
            ),
        )

        panel_width = min(console.width, 88)
        console.print(
            Panel(
                Markdown(welcome_text),
                border_style=current_mode.color,
                width=panel_width,
            )
        )
        console.print()

    def clear_history(self) -> None:
        """Clear conversation history and start a fresh session."""
        self.tool_approval_helper.clear_session_rules()
        self.conversation_history = []
        self.token_tracker.reset()
        console.print("[dim]Conversation history cleared[/dim]")
        console.print()

    def restore_engagement(
        self, engagement_id: str, *, clear_session_rules: bool = False
    ) -> Optional[Engangement]:
        """Restore CLI state from a saved engagement.

        Pure state restoration — callers handle any console output. Returns the
        loaded engagement (so callers can inspect metadata), or None if the
        engagement does not exist. Raises `EngagementRestoreError` when the
        saved metadata names a mode, verbosity or approval mode this build no
        longer has. Set clear_session_rules=True for mid-session resumes
        (e.g. /resume) to drop approval rules from the prior session.
        """
        engagement = self.engagement_store.load_engagement(engagement_id)
        if engagement is None:
            return None

        metadata = engagement.metadata
        mode_type = restored_setting(
            ModeType, metadata.mode, engagement_id, "mode", by_name=True
        )
        verbosity = restored_setting(
            Verbosity, metadata.verbosity, engagement_id, "verbosity"
        )
        approval_mode = restored_setting(
            ApprovalMode, metadata.approval_mode, engagement_id, "approval mode"
        )

        # Validated before anything is mutated, so a bad value cannot leave the
        # CLI half-restored.
        if clear_session_rules:
            self.tool_approval_helper.clear_session_rules()

        self.mode_manager.switch_mode(mode_type=mode_type)

        self.verbosity_manager.set(verbosity)
        self.approval_mode_manager.set(approval_mode)

        self.conversation_history = engagement.messages
        apply_verbosity_to_system_prompt(self.conversation_history, verbosity)

        workspace_path = self.workspace_manager.activate(engagement.metadata.id)
        self.tool_approval_helper.set_workspace(workspace_path)
        self._open_knowledge_base(engagement.metadata.id)

        return engagement

    async def get_response(self, user_message: Optional[str] = None) -> None:
        """
        Get response from LLM provider with tool execution loop.

        Args:
            user_message: User's input message
        """

        # Pressing Enter on an empty prompt is a UI no-op. In particular, do
        # not create a workspace/system-only history and send Codex an empty
        # Responses API input array, which the backend rejects with HTTP 400.
        if user_message is not None and not user_message.strip():
            return

        if not self.provder_manager.provider:
            console.print(
                "[bold yellow]No provider is currently initialized.[/bold yellow]",
            )
            console.print(
                "[dim]Use /provider <name> to switch. Selecting Codex starts "
                "browser sign-in when needed.[/dim]",
            )
            console.print()
            return

        self._apply_system_prompt_if_possible()

        # Turn boundary: agents that finished while the user was typing report
        # in before the new message, which is the order they happened in.
        self._deliver_agent_reports()

        if user_message:
            self._append_message_to_conversation(
                Message(role="user", content=user_message),
            )

        await self._ensure_compactor()

        session = LoopSession(
            provider=self.provder_manager.provider,
            history=self.conversation_history,
            tools=self.tools,
            approval=InteractiveApproval(
                self.tool_approval_helper, self.approval_mode_manager
            ),
            output=self.output_sink,
            max_iterations=100,
            temperature=self.config.agent_temperature,
            max_response_tokens=self.config.max_response_tokens,
            engagement_store=self.engagement_store,
            knowledge_base=self._knowledge_base,
            ledger=self._findings_ledger,
            kb_clip_threshold_bytes=self.config.kb_clip_threshold_bytes,
            tool_output_max_chars=self.config.tool_output_max_chars,
            tool_output_head_chars=self.config.tool_output_head_chars,
            tool_output_tail_chars=self.config.tool_output_tail_chars,
            compactor=self.compactor,
            token_tracker=self.token_tracker,
            interrupt=self.input_manager,
            transient_prompts=list(self._transient_system_prompts),
            compaction_warning_shown=self._compaction_warning_shown,
        )

        self._start_switcher_watch()
        try:
            await self.engine.run_loop(session)
        except Exception as e:
            logger.error(f"Exception in KerkovCLI.get_response: {e}")
            # Through the sink, not the console: a turn that fails while the
            # user is attached to an agent must not jump the buffer either.
            self.output_sink.error(f"Error: {escape(str(e))}")
            from kerkov.providers.codex import CodexModelNotAvailableError

            if isinstance(e, CodexModelNotAvailableError):
                return
            import traceback

            self.output_sink.info(escape(traceback.format_exc()))
        finally:
            await self._stop_switcher_watch()
            self._compaction_warning_shown = session.compaction_warning_shown

    def _start_switcher_watch(self) -> None:
        """Listen for a mid-turn Ctrl-O for as long as the turn runs."""
        if self._switcher_task is not None:
            return
        self.input_manager.switcher_requested.clear()
        self._switcher_task = asyncio.create_task(self._watch_for_switcher())

    async def _stop_switcher_watch(self) -> None:
        task, handoff = self._switcher_task, self._switcher_handoff
        self._switcher_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"Agent switcher watch failed: {exc}")
        if handoff is not None:
            # The picker is shielded from the watcher's cancellation: the turn
            # finishing is no reason to yank a menu out from under the user.
            try:
                await handoff
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(f"Agent switcher handoff failed: {exc}")
        # The handoff restores the Escape watcher when the turn was mid-await;
        # nothing stops it once the turn is over, and prompt_toolkit is next.
        self.input_manager.stop_watching_for_interrupt()

    async def _watch_for_switcher(self) -> None:
        """Open the agent picker whenever Ctrl-O lands during a turn.

        `_await_interruptible` rebuilds the cbreak watcher around every awaited
        coroutine, so the request arrives on the manager rather than from a
        thread this could join; the turn keeps running underneath the picker.
        """
        while True:
            await self.input_manager.switcher_requested.wait()
            self._switcher_handoff = asyncio.create_task(self._open_switcher())
            try:
                await asyncio.shield(self._switcher_handoff)
            finally:
                self._switcher_handoff = None

    async def _open_switcher(self) -> None:
        """Hand stdin to the picker and give it back exactly as it was."""
        with self.output_sink.suspend_status():
            # Claim inside the handoff so checking availability and taking
            # ownership are atomic with respect to approval takeovers.
            async with self.input_manager.picker_ownership():
                await self._execute_user_message_safely(AgentsMessage())

    def _apply_system_prompt_if_possible(self) -> None:
        """
        Applies system prompt if this is the first message in a conversation.
        Also starts a new persistence engagement if needed.
        """
        if len(self.conversation_history) == 0:
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

            system_message = Message(role="system", content=system_prompt)

            model = (
                self.provder_manager.provider.model
                if self.provder_manager.provider
                else "unknown"
            )
            engagement_id = self.engagement_store.start_engagement(
                mode=self.mode_manager.current_mode.name,
                model=model,
                verbosity=self.verbosity_manager.current.value,
                approval_mode=self.approval_mode_manager.current.value,
            )
            workspace_path = self.workspace_manager.activate(engagement_id)
            self.tool_approval_helper.set_workspace(workspace_path)
            self._open_knowledge_base(engagement_id)
            console.print(f"[dim]Workspace: {workspace_path}[/dim]")
            self._append_message_to_conversation(system_message)

    def _open_knowledge_base(self, engagement_id: str) -> None:
        """Open the knowledge base for the engagement tree `engagement_id` is in.

        Resolves to the root first: resuming an agent sub-run should reattach to
        its master's store, not mint a second one.
        """
        if not self.config.kb_enabled:
            return
        try:
            root_id = resolve_root_engagement_id(
                engagement_id, self.engagement_store.parent_engagement_id_of
            )
            if (
                self._knowledge_base is not None
                and self._knowledge_base.root_engagement_id == root_id
            ):
                return
            if self._knowledge_base is not None:
                self._knowledge_base.close()
            backend = (
                resolve_embedding_backend(self.config.kb_embedding_provider)
                if self.config.kb_embeddings
                else None
            )
            self._knowledge_base = open_knowledge_base(
                root_id,
                self.engagement_store.store_dir,
                embedding_backend=backend,
            )
            self._findings_ledger = FindingsLedger(self._knowledge_base)
            logger.info(f"Knowledge base ready: {self._knowledge_base.path}")
        except Exception as e:
            # Recall is an enhancement; a broken store must not stop the CLI.
            logger.warning(f"Could not open knowledge base for {engagement_id}: {e}")
            self._knowledge_base = None
            self._findings_ledger = None

    def get_resume_command_message(self) -> str:
        """Return the resume command for the current engagement, if one exists."""
        engagement_id = self.engagement_store.engagement_id
        if not engagement_id:
            return "No active engagement has been saved yet, so there is no resume command."
        return f"Resume this session with: uv run kerkov --resume {engagement_id}"

    def print_resume_command(self) -> None:
        """Print the resume command after an interactive session exits."""
        if getattr(self, "_resume_command_printed", False):
            return
        self._resume_command_printed = True
        console.print(f"[dim]{self.get_resume_command_message()}[/dim]")

    def quit(self) -> None:
        self.print_resume_command()
        sys.exit(0)
