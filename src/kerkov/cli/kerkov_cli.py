#!/usr/bin/env python3
"""
Kerkov Bot - Security-focused conversational AI CLI
Interactive CLI for the Kerkov Bot
"""

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.agents.agent_tool import SpawnAgentTool
from kerkov.cli.approval_mode_manager import ApprovalMode, ApprovalModeManager
from kerkov.cli.approval_rules import ApprovalRulesEngine, default_approvals_source
from kerkov.cli.autonomy_manager import AutonomyLevel, AutonomyManager, preset_for
from kerkov.cli.engagement_store import EngagementStore, Engangement
from kerkov.cli.input_manager import InputManager
from kerkov.cli.input_parser import GeneralMessage, InputParser
from kerkov.cli.messages.user_message import UserMessage
from kerkov.cli.mode_manager import ModeManager
from kerkov.cli.modes import ModeType
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
from kerkov.core.config_manager import get_config, save_user_setting
from kerkov.core.credentials import (
    CredentialInput,
    CredentialManager,
    CredentialSpec,
    credential_storage_description,
    get_credential_manager,
    provider_credential_spec,
)
from kerkov.core.conversation_engine import (
    ConsoleSink,
    ConversationEngine,
    InteractiveApproval,
    LoopSession,
)
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.run_workspace import RunWorkspaceManager, get_current_workspace
from kerkov.core.shell import ShellManager
from kerkov.models.basic_models import Message
from kerkov.providers import MissingCredentialError, list_providers
from kerkov.providers.provider_manager import ProviderManager
from kerkov.tools.base_tool import BaseTool
from kerkov.tools.http_profile_loader import load_http_profiles
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

SETUP_LATER_PROVIDER = "__setup_later__"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
PROVIDER_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "nvidia": "NVIDIA",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
    "codex": "Codex",
}


def _normalize_ollama_base_url(value: str) -> str:
    """Validate and normalize an Ollama API origin."""

    entered = value.strip()
    try:
        parsed = urlsplit(entered)
        parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid Ollama server URL.") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Ollama server URL must start with http:// or https://.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama server URL must not contain a username or password.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            "Ollama server URL must be an origin without a path, query, or fragment."
        )

    return parsed._replace(scheme=parsed.scheme.lower(), path="").geturl()


class KerkovCLI:
    """Interactive CLI for Kerkov Bot."""

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
        self.output_sink = ConsoleSink(console)

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
        self.shell_manager = ShellManager()
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
        # Master tools snapshot — agents draw their tool set from here. The
        # spawn tool itself is appended afterward and intentionally not in
        # the snapshot (agents do NOT receive a copy of the snapshot; they
        # receive a SpawnAgentTool reconstructed at their depth, see
        # `agent_tool.py`).
        master_tools_snapshot = list(self.tools)
        self.tools.append(
            SpawnAgentTool(
                agent_manager=self.agent_manager,
                master_tools=master_tools_snapshot,
                provider_getter=lambda: self.provder_manager.provider,
                registry=self.agent_registry,
                parent_depth=0,
                max_batch=self.config.agent_max_batch,
                max_iterations=self.config.agent_max_iterations,
                master_engagement_store=self.engagement_store,
                temperature=self.config.agent_temperature,
                max_response_tokens=self.config.max_response_tokens,
            )
        )
        self.token_tracker = TokenTracker()
        self.compactor: Optional[ContextCompactor] = None
        self._compaction_warning_shown = False
        self._transient_system_prompts: List[str] = []
        self.engine = ConversationEngine()
        self._initialize_provider()

    async def prompt_for_credential(
        self,
        spec: CredentialSpec,
        persist: bool = True,
    ) -> Optional[CredentialInput]:
        """Collect a credential outside chat using password-style input."""

        output_sink = getattr(self, "output_sink", None)
        status_suspension = (
            output_sink.suspend_status()
            if output_sink is not None
            else contextlib.nullcontext()
        )
        with status_suspension:
            was_watching = self.input_manager.is_watching_for_interrupt
            if was_watching:
                self.input_manager.stop_watching_for_interrupt()

            self.console.print()
            self.console.print(
                f"[bold cyan]{spec.label} required[/bold cyan]\n"
                "[dim]Input is masked and will not be added to chat history or logs.[/dim]"
            )
            try:
                value = await self.input_manager.get_secret(
                    f"<b>{spec.label}:</b> ",
                )
            except (KeyboardInterrupt, EOFError):
                self.console.print("[yellow]Credential entry cancelled.[/yellow]")
                return None
            finally:
                if was_watching:
                    self.input_manager.reset_interrupt()
                    self.input_manager.start_watching_for_interrupt()

            value = value.strip()
            if not value:
                self.console.print(
                    "[yellow]Credential was empty; nothing changed.[/yellow]"
                )
                return None
            return CredentialInput(value=value, persist=persist)

    async def prompt_for_ollama_base_url(self) -> Optional[str]:
        """Collect an Ollama API endpoint outside normal chat history."""

        output_sink = getattr(self, "output_sink", None)
        status_suspension = (
            output_sink.suspend_status()
            if output_sink is not None
            else contextlib.nullcontext()
        )
        with status_suspension:
            was_watching = self.input_manager.is_watching_for_interrupt
            if was_watching:
                self.input_manager.stop_watching_for_interrupt()

            configured = get_config(reload=True).ollama_base
            default_url = configured or DEFAULT_OLLAMA_BASE_URL
            self.console.print()
            self.console.print(
                "[bold cyan]Ollama server URL required[/bold cyan]\n"
                "[dim]Enter the Ollama API endpoint. For a local server, keep "
                f"{DEFAULT_OLLAMA_BASE_URL}.[/dim]"
            )
            try:
                while True:
                    try:
                        value = await self.input_manager.get_text(
                            "<b>Ollama server URL:</b> ",
                            default=default_url,
                        )
                    except (KeyboardInterrupt, EOFError):
                        self.console.print(
                            "[yellow]Ollama server entry cancelled.[/yellow]"
                        )
                        return None

                    try:
                        return _normalize_ollama_base_url(value or default_url)
                    except ValueError as exc:
                        self.console.print(f"[yellow]{exc}[/yellow]")
                        default_url = value.strip() or default_url
            finally:
                if was_watching:
                    self.input_manager.reset_interrupt()
                    self.input_manager.start_watching_for_interrupt()

    async def activate_provider_interactive(
        self,
        provider_name: str,
        *,
        validate_existing: bool = False,
    ) -> bool:
        """Prompt for a missing key, validate it, and activate the provider."""

        normalized_provider = provider_name.strip().lower()
        ollama_base: Optional[str] = None
        if normalized_provider == "ollama":
            ollama_base = await self.prompt_for_ollama_base_url()
            if ollama_base is None:
                return False

        spec = provider_credential_spec(provider_name)
        prompted: Optional[CredentialInput] = None
        if spec is not None and not self.credentials.get(spec):
            prompted = await self.prompt_for_credential(spec, True)
            if prompted is None:
                return False
            self.credentials.set_session(spec, prompted.value)

        try:
            if ollama_base is not None:
                candidate = self.provder_manager.build_provider(
                    provider_name,
                    config_overrides={"ollama_base": ollama_base},
                )
            else:
                candidate = self.provder_manager.build_provider(provider_name)
        except Exception as exc:
            if prompted is not None and spec is not None:
                self.credentials.forget_session(spec)
            self.console.print(f"[bold red]Error:[/bold red] {exc}")
            self.console.print()
            return False

        if (
            prompted is not None
            or validate_existing
            or ollama_base is not None
            or normalized_provider == "codex"
        ):
            try:
                valid = await asyncio.to_thread(candidate.validate_connection)
            except Exception:
                valid = False
            if not valid:
                if prompted is not None and spec is not None:
                    self.credentials.forget_session(spec)
                provider_label = PROVIDER_DISPLAY_NAMES.get(
                    provider_name.strip().lower(),
                    provider_name,
                )
                self.console.print(
                    f"[bold red]Error:[/bold red] {provider_label} "
                    "could not be validated."
                )
                self.console.print()
                return False

        if prompted is not None:
            assert spec is not None
            save_result = self.credentials.save(
                spec,
                prompted.value,
                persist=prompted.persist,
            )
            if save_result.warning:
                self.console.print(f"[yellow]{save_result.warning}[/yellow]")
            elif save_result.persisted:
                self.console.print(
                    "[dim]Credential saved in the "
                    f"{credential_storage_description(save_result.storage)}.[/dim]"
                )

        if ollama_base is not None:
            try:
                settings_path = save_user_setting("OLLAMA_BASE", ollama_base)
            except Exception:
                self.console.print(
                    "[yellow]The Ollama server URL could not be saved; it will "
                    "remain active for this session only.[/yellow]"
                )
            else:
                self.console.print(
                    f"[dim]Ollama server URL saved in {settings_path}.[/dim]"
                )

        self.provder_manager.set_provider(candidate)
        await self._reconfigure_context_for_provider()
        model_selection_notice = getattr(candidate, "model_selection_notice", None)
        if isinstance(model_selection_notice, str) and model_selection_notice:
            self.console.print(f"[yellow]{model_selection_notice}[/yellow]")
        provider_label = str(candidate.get_model_info()["provider"]).replace(
            "Provider", ""
        )
        self.console.print(f"[green]✓[/green] Switched provider to {provider_label}")
        self.console.print()
        return True

    def _initialize_provider(self) -> None:
        """Initialize the LLM provider based on configuration."""
        try:
            self.provder_manager.initialize_provider()
            provider = self.provder_manager.provider

            if not provider:
                configured = self.config.llm_provider
                console.print(
                    f"[bold yellow]Warning:[/bold yellow] No LLM provider initialized (configured: {configured})."
                )
                console.print(
                    "[dim]You can authenticate (e.g. /codex-login) then run /provider codex.[/dim]"
                )
                return

            if not provider.validate_connection():
                console.print(
                    "[bold yellow]Provider unavailable:[/bold yellow] "
                    "The configured LLM provider could not be reached."
                )
                self.provder_manager.clear_provider()
                return

            model_selection_notice = getattr(
                provider,
                "model_selection_notice",
                None,
            )
            if isinstance(model_selection_notice, str) and model_selection_notice:
                console.print(f"[yellow]{model_selection_notice}[/yellow]")

            console.print(
                f"[green]✓[/green] Connected to {provider.model}",
                style="dim",
            )

        except MissingCredentialError as e:
            logger.info(
                "Configured provider is waiting for interactive credential setup "
                "(provider=%s)",
                e.provider_name,
            )
            return
        except Exception as e:
            configured = self.config.llm_provider
            console.print(f"[bold red]Error:[/bold red] {str(e)}", style="bold red")
            if configured.strip().lower() == "codex":
                console.print(
                    "[dim]Starting without a provider so you can run /codex-login, then /provider codex.[/dim]"
                )
                return
            logger.error(f"Exception occurred in KerkovCLI._initialize_provider: {e}")
            sys.exit(1)

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
        if self.compactor is None:
            self.compactor = ContextCompactor(
                token_tracker=self.token_tracker,
                max_context_tokens=max_ctx,
                threshold_pct=self.config.compaction_threshold_pct,
                summary_reserve_tokens=self.config.summary_reserve_tokens,
            )
        else:
            self.compactor.update_limits(max_ctx)
        self._compaction_warning_shown = False

    def _append_message_to_conversation(self, msg: Message) -> None:
        """Append a message to history and engagement store."""
        self.conversation_history.append(msg)
        self.engagement_store.append_message(msg)

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
                mode = self.mode_manager.current_mode
                prompt_html = (
                    f'<style fg="{mode.color}">{mode.name[0]}</style>'
                    f' <b><style fg="cyan">You:</style></b> '
                )
                user_input = await self.input_manager.get_line(prompt_html)
                user_message = InputParser.parse_for_message_type(user_input)

                logger.info(f"User message parsed: type:{type(user_message)}")
                if isinstance(user_message, GeneralMessage):
                    logger.info(f"User typed: {user_message.user_input}")

                await self._execute_user_message_safely(user_message)
            except KeyboardInterrupt:
                console.print("\n[dim]Exiting due to keyboard interruption[/dim]")
                self.print_resume_command()
                break
            except EOFError:
                console.print("\n[dim]Exiting due to end of file error[/dim]")
                self.print_resume_command()
                break

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
        finally:
            self.tool_approval_helper.non_interactive_auto_approve = False
            if self._transient_system_prompts:
                self._transient_system_prompts.pop()

    def current_provider_name(self) -> Optional[str]:
        """Return the canonical name of the active provider, if configured."""

        provider = self.provder_manager.provider
        if provider is None:
            return None
        provider_name = str(provider.get_model_info()["provider"])
        return provider_name.replace("Provider", "").strip().lower()

    async def select_provider_interactive(
        self,
        *,
        allow_setup_later: bool = False,
    ) -> Optional[str]:
        """Choose a provider, highlighting the active one when present."""

        current_provider = self.current_provider_name()
        current_model = None
        if current_provider is not None:
            current_model = str(self.provder_manager.provider.get_model_info()["model"])
        options: list[tuple[str, str]] = []
        for provider_name in list_providers():
            if provider_name == current_provider:
                status = f"current · {current_model}"
            else:
                spec = provider_credential_spec(provider_name)
                if spec is not None:
                    status = (
                        "credential configured"
                        if self.credentials.get(spec)
                        else "API key required"
                    )
                elif provider_name == "ollama":
                    status = "server URL required; no API key"
                elif provider_name == "codex":
                    status = "browser sign-in"
                else:
                    status = "no API key"
            options.append(
                (
                    provider_name,
                    f"{PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)} "
                    f"— {status}",
                )
            )

        if allow_setup_later:
            options.append(
                (
                    SETUP_LATER_PROVIDER,
                    "Continue without a provider — setup mode",
                )
            )
        selected = await self.input_manager.choose(
            "Select an LLM provider",
            options,
            default=(current_provider or self.config.llm_provider.strip().lower()),
        )
        if selected in {None, SETUP_LATER_PROVIDER}:
            return None
        return selected

    async def _select_startup_provider(self) -> Optional[str]:
        """Offer provider onboarding when startup has no usable default."""

        return await self.select_provider_interactive(allow_setup_later=True)

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

    def _display_provider_setup_guidance(self) -> None:
        """Explain the usable CLI state after default-provider setup is skipped."""

        self.console.print()
        self.console.print(
            "[bold yellow]No LLM provider is active; Kerkov is in setup mode.[/bold yellow]"
        )
        self.console.print(
            "[dim]Use `/provider` to open the provider picker, or "
            "`/provider <name>` to select one directly. Commands such as "
            "`/help`, `/auth`, and `/q` remain available.[/dim]"
        )
        self.console.print()

    def _display_welcome(self) -> None:
        """Display welcome message."""
        current_mode = self.mode_manager.current_mode
        current_autonomy = self.autonomy_manager.current_level
        welcome_text = """
# Kerkov Bot

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
        engagement does not exist. Set clear_session_rules=True for mid-session
        resumes (e.g. /resume) to drop approval rules from the prior session.
        """
        engagement = self.engagement_store.load_engagement(engagement_id)
        if engagement is None:
            return None

        if clear_session_rules:
            self.tool_approval_helper.clear_session_rules()

        self.mode_manager.switch_mode(
            mode_type=ModeType[engagement.metadata.mode.upper()]
        )

        verbosity = Verbosity(engagement.metadata.verbosity)
        self.verbosity_manager.set(verbosity)
        self.approval_mode_manager.set(ApprovalMode(engagement.metadata.approval_mode))

        self.conversation_history = engagement.messages
        apply_verbosity_to_system_prompt(self.conversation_history, verbosity)

        workspace_path = self.workspace_manager.activate(engagement.metadata.id)
        self.tool_approval_helper.set_workspace(workspace_path)

        return engagement

    async def get_response(self, user_message: Optional[str] = None) -> None:
        """
        Get response from LLM provider with tool execution loop.

        Args:
            user_message: User's input message
        """

        if not self.provder_manager.provider:
            console.print(
                "[bold yellow]No provider is currently initialized.[/bold yellow]",
            )
            console.print(
                "[dim]Use /provider <name> to switch, or /codex-login then /provider codex.[/dim]",
            )
            console.print()
            return

        self._apply_system_prompt_if_possible()

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
            compactor=self.compactor,
            token_tracker=self.token_tracker,
            interrupt=self.input_manager,
            transient_prompts=list(self._transient_system_prompts),
            compaction_warning_shown=self._compaction_warning_shown,
        )

        try:
            await self.engine.run_loop(session)
        except Exception as e:
            logger.error(f"Exception in KerkovCLI.get_response: {e}")
            console.print(f"[bold red]Error:[/bold red] {str(e)}", style="bold red")
            from kerkov.providers.codex import CodexModelNotAvailableError

            if isinstance(e, CodexModelNotAvailableError):
                return
            import traceback

            console.print(f"[dim]{traceback.format_exc()}[/dim]")
        finally:
            self._compaction_warning_shown = session.compaction_warning_shown

    def _apply_system_prompt_if_possible(self) -> None:
        """
        Applies system prompt if this is the first message in a conversation.
        Also starts a new persistence engagement if needed.
        """
        if len(self.conversation_history) == 0:
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
            console.print(f"[dim]Workspace: {workspace_path}[/dim]")
            self._append_message_to_conversation(system_message)

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

    def quit(self):
        self.print_resume_command()
        sys.exit(0)
