#!/usr/bin/env python3
"""
Kerkov Bot - Security-focused conversational AI CLI
Interactive CLI for the Kerkov Bot
"""

import sys
from typing import List, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from src.core.logging.logger_factory import get_logger
from src.cli.approval_mode_manager import ApprovalMode, ApprovalModeManager
from src.cli.autonomy_manager import AutonomyManager, preset_for, AutonomyLevel
from src.cli.verbosity_manager import Verbosity, VerbosityManager
from src.cli.verbosity_prompts import VERBOSITY_MARKER, VERBOSITY_PROMPTS
from src.cli.engagement_store import EngagementStore
from src.cli.input_manager import InputManager
from src.cli.input_parser import InputParser, GeneralMessage
from src.cli.mode_manager import ModeManager
from src.cli.modes import ModeType
from src.cli.ctf_context import CTFContext
from src.cli.tool_approval_helper import ToolApprovalCliHelper
from src.agents.agent_manager import AgentManager
from src.agents.agent_registry import AgentRegistry
from src.agents.agent_tool import SpawnAgentTool
from src.context.token_tracker import TokenTracker
from src.context.context_compactor import ContextCompactor
from src.core.config_manager import get_config
from src.core.conversation_engine import (
    ConsoleSink,
    ConversationEngine,
    InteractiveApproval,
    LoopSession,
)
from src.core.run_workspace import RunWorkspaceManager, get_current_workspace
from src.models.basic_models import Message
from src.providers.provider_manager import ProviderManager
from src.context.skills.skill_manager import SkillManager
from src.tools.base_tool import BaseTool
from src.tools.http_profile_loader import load_http_profiles
from src.tools.skill_tool import SkillTool
from src.tools.tool_suite import ToolSuite

from src.core.shell import ShellManager

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


class KerkovCLI:
    """Interactive CLI for Kerkov Bot."""

    def __init__(self, mode: Optional[ModeType] = None):
        """
        Initialize the CLI.

        Args:
            mode: Initial operational mode (defaults to AUDIT)
        """
        self.config = get_config()
        self.conversation_history: List[Message] = []
        self.provder_manager = ProviderManager(self.config)
        self.mode_manager = ModeManager(initial_mode=mode)

        # Derive verbosity + approval_mode from the autonomy preset, then
        # override with the explicit axis env vars if set.
        preset_verbosity, preset_approval = preset_for(
            AutonomyLevel(self.config.autonomy_level)
        )
        initial_verbosity = (
            Verbosity(self.config.verbosity) if self.config.verbosity else preset_verbosity
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
        self.ctf_context = CTFContext()
        self.input_manager = InputManager(console)
        self.tool_approval_helper = ToolApprovalCliHelper(self.input_manager)
        self.shell_manager = ShellManager()
        self.workspace_manager = RunWorkspaceManager(self.config.workspace_dir)
        self.skill_manager = SkillManager()
        self.agent_manager = AgentManager()
        self.agent_registry = AgentRegistry(
            max_depth=self.config.agent_max_depth,
            max_total=self.config.agent_max_total,
        )
        http_profiles = load_http_profiles(self.config.http_profiles_path)
        self.tools: List[BaseTool] = ToolSuite.get_tools(http_profiles)
        self.tools.append(SkillTool(self.skill_manager))
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

        self.console = console

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
                    "[bold red]Error:[/bold red] Failed to connect to LLM provider",
                    style="bold red",
                )
                console.print("Please check your provider configuration and connection")
                sys.exit(1)

            console.print(
                f"[green]✓[/green] Connected to {provider.model}",
                style="dim",
            )

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

        max_ctx = await provider.get_max_context_tokens() or self.config.max_context_tokens
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

                await user_message.execute(self)
            except KeyboardInterrupt:
                console.print("\n[dim]Exiting due to keyboard interruption[/dim]")
                self.print_resume_command()
                break
            except EOFError:
                console.print("\n[dim]Exiting due to end of file error[/dim]")
                self.print_resume_command()
                break

    async def run_non_interactive(self, inputs: List[str], *, show_welcome: bool = False) -> None:
        """Process one or more inputs without starting the prompt loop."""
        if show_welcome:
            self._display_welcome()

        self.tool_approval_helper.non_interactive_auto_approve = True
        self._transient_system_prompts.append(NON_INTERACTIVE_EXECUTION_PROMPT)
        try:
            for raw_input in inputs:
                user_message = InputParser.parse_for_message_type(raw_input)
                await user_message.execute(self)
        finally:
            self.tool_approval_helper.non_interactive_auto_approve = False
            if self._transient_system_prompts:
                self._transient_system_prompts.pop()

    def _display_welcome(self) -> None:
        """Display welcome message."""
        current_mode = self.mode_manager.current_mode
        welcome_text = """
# Kerkov Bot

Security-focused conversational AI assistant for penetration testing and security analysis.

**Current Mode:** {mode_name} - {mode_desc}
**Autonomy:** Level {autonomy_level} ({autonomy_name})
**Verbosity:** {verbosity}  **Approval:** {approval_mode}
**Workspace:** {workspace}

**Commands:**
- Type your message and press Enter
- `exit`, `quit`, or `/q` to exit
- `clear` to clear conversation history
- `/mode` to show current mode
- `/mode [recon|audit|offensive|ctf]` to switch modes
- `/autonomy` to show/set the preset (1-4) — sets both verbosity & approval at once
- `/verbosity [guided|standard|aggressive|silent]` to change narration only
- `/approval [strict|normal|permissive|yolo]` to change approval gating only
- `/yolo [on|off]` to toggle the approval bypass (sugar for `/approval yolo|normal`)
- `/ctf-info` to view/update CTF challenge information (CTF mode only)
- `/model [name]` to switch LLM models
- `/models` to list available models for the current provider
- `/provider [name]` to switch LLM providers
- `/providers` to list supported providers
- `/skill` to list available skills
- `/skill <name> [args]` to load a skill with optional input
- `/skill search <query>` to search skills
- `/engagements` to list saved engagements
- `/resume [id]` to resume a previous engagement
- `/codex-login` to authenticate Codex using browser OAuth

**Model:** {model}
        """.format(
            mode_name=current_mode.name,
            mode_desc=current_mode.description,
            autonomy_level=self.autonomy_manager.current_level.value,
            autonomy_name=self.autonomy_manager.current_level.name.capitalize(),
            verbosity=self.verbosity_manager.current.value.capitalize(),
            approval_mode=self.approval_mode_manager.current.value.capitalize(),
            workspace=str(get_current_workspace())
            if get_current_workspace()
            else "created after first message",
            model=self.provder_manager.provider.model if self.provder_manager.provider else "N/A",
        )

        console.print(Panel(Markdown(welcome_text), border_style=current_mode.color))
        console.print()    

    def clear_history(self) -> None:
        """Clear conversation history and start a fresh session."""
        self.tool_approval_helper.clear_optional_approvals()
        self.conversation_history = []
        self.token_tracker.reset()
        console.print("[dim]Conversation history cleared[/dim]")
        console.print()

    async def get_response(self, user_message: Optional[str] = None) -> None:
        """
        Get response from LLM provider with tool execution loop.

        Args:
            user_message: User's input message
        """

        if not self.provder_manager.provider:
            console.print("[bold yellow]No provider is currently initialized.[/bold yellow]")
            console.print("[dim]Use /provider <name> to switch, or /codex-login then /provider codex.[/dim]")
            console.print()
            return

        self._apply_system_prompt_if_possible()

        if user_message:
            self._append_message_to_conversation(
                Message(role="user", content=user_message)
            )

        await self._ensure_compactor()

        session = LoopSession(
            provider=self.provder_manager.provider,
            history=self.conversation_history,
            tools=self.tools,
            approval=InteractiveApproval(
                self.tool_approval_helper, self.approval_mode_manager
            ),
            output=ConsoleSink(console),
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

            if self.mode_manager.current_mode_type == ModeType.CTF and self.ctf_context.has_context():
                system_prompt = system_prompt + "\n" + self.ctf_context.to_prompt_string()

            skill_index = self.skill_manager.get_index_summary()
            if skill_index:
                system_prompt += "\n\n" + skill_index

            agent_index = self.agent_manager.get_index_summary()
            if agent_index:
                system_prompt += "\n\n" + agent_index

            system_prompt += VERBOSITY_MARKER + VERBOSITY_PROMPTS[self.verbosity_manager.current]

            system_message = Message(role="system", content=system_prompt)

            model = self.provder_manager.provider.model if self.provder_manager.provider else "unknown"
            engagement_id = self.engagement_store.start_engagement(
                mode=self.mode_manager.current_mode.name,
                model=model,
                verbosity=self.verbosity_manager.current.value,
                approval_mode=self.approval_mode_manager.current.value,
            )
            workspace_path = self.workspace_manager.activate(engagement_id)
            console.print(f"[dim]Workspace: {workspace_path}[/dim]")
            self._append_message_to_conversation(system_message)

    async def prompt_ctf_context(self) -> None:
        """Prompt user for optional CTF challenge information."""
        console.print(
            Panel(
                "[bold cyan]CTF Challenge Information[/bold cyan]\n\n"
                "Provide optional context about the CTF challenge.\n"
                "Press Enter to skip any field, or type 'skip' to skip all.\n"
                "You can update this later with [bold]/ctf-info[/bold]",
                border_style="cyan",
            )
        )
        console.print()

        provide_info = (await self.input_manager.get_line('<style fg="cyan">Would you like to provide challenge information? (y/n):</style> ')).strip().lower()

        if provide_info not in ['y', 'yes']:
            console.print("[dim]Skipping challenge information. Use /ctf-info to add it later.[/dim]")
            console.print()
            return

        self.ctf_context.clear()

        console.print()
        challenge_name = (await self.input_manager.get_line('<style fg="cyan">Challenge Name:</style> ')).strip()
        if challenge_name:
            self.ctf_context.challenge_name = challenge_name

        category = (await self.input_manager.get_line('<style fg="cyan">Category (web exploitation/crypto/binary exploitation/forensics/reverse engineering/misc):</style> ')).strip()
        if category:
            self.ctf_context.category = category

        target = (await self.input_manager.get_line('<style fg="cyan">Target (URI/file path/IP):</style> ')).strip()
        if target:
            self.ctf_context.target = target

        console.print("[cyan]Description (press Enter twice when done):[/cyan]")
        description_lines = []
        while True:
            line = await self.input_manager.get_line()
            if not line:
                break
            description_lines.append(line)

        if description_lines:
            self.ctf_context.description = "\n".join(description_lines)

        console.print("[cyan]Additional Notes (press Enter twice when done):[/cyan]")
        notes_lines = []
        while True:
            line = await self.input_manager.get_line()
            if not line:
                break
            notes_lines.append(line)

        if notes_lines:
            self.ctf_context.additional_notes = "\n".join(notes_lines)

        gen_writeup = (await self.input_manager.get_line('<style fg="cyan">Would you like for kerkov to generate a CTF writeup? (y/n):</style> ')).strip().lower()

        if gen_writeup in ['y', 'yes']:
            console.print("[cyan]The name of the writeup will be the challenge name or an apt generated title if the challenge name contains illegal characters.[/cyan]")
            console.print("[cyan]Specify the directory for the markdown writeup to be generated in. (Press 'enter' to use the engagement workspace)[/cyan]")
            dir = (await self.input_manager.get_line('<style fg="cyan">Directory:</style> ')).strip()
            self.ctf_context.write_up_dir = dir if dir else "."

        console.print()
        if self.ctf_context.has_context():
            logger.info(f"Given CTF context: {self.ctf_context}")

            console.print("[green]✓ Challenge information saved![/green]")
            console.print("[dim]The AI will receive this context with your messages.[/dim]")
        else:
            console.print("[dim]No information provided. You can add it later with /ctf-info[/dim]")
        console.print()
    
    def get_resume_command_message(self) -> str:
        """Return the resume command for the current engagement, if one exists."""
        engagement_id = self.engagement_store.engagement_id
        if not engagement_id:
            return "No active engagement has been saved yet, so there is no resume command."
        return f"Resume this session with: python -m src.cli.main --resume {engagement_id}"

    def print_resume_command(self) -> None:
        """Print the resume command after an interactive session exits."""
        if getattr(self, "_resume_command_printed", False):
            return
        self._resume_command_printed = True
        console.print(f"[dim]{self.get_resume_command_message()}[/dim]")

    def quit(self):
        self.print_resume_command()
        sys.exit(0)
