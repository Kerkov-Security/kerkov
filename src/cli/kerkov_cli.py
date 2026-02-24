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

from src.cli.input_parser import InputParser
from src.cli.mode_manager import ModeManager
from src.cli.modes import ModeType
from src.cli.ctf_context import CTFContext
from src.cli.tool_approval_helper import ToolApprovalCliHelper
from src.core.config_manager import get_config
from src.models.basic_models import Message
from src.providers.provider_manager import ProviderManager
from src.tools.base_tool import BaseTool, ToolResult
from src.tools.tool_suite import ToolSuite

from src.core.shell import ShellManager

console = Console()

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
        self.ctf_context = CTFContext()
        self.tool_approval_helper = ToolApprovalCliHelper()
        self.shell_manager = ShellManager()
        self.tools: List[BaseTool] = ToolSuite.get_tools(self.shell_manager)

        self._initialize_provider()

        self.console = console

    def _initialize_provider(self) -> None:
        """Initialize the LLM provider based on configuration."""
        try:
            self.provder_manager.initialize_provider()
            provider = self.provder_manager.provider

            if not provider:
                console.print(
                    "[bold red]Error:[/bold red] Failed to initialize LLM provider",
                    style="bold red",
                )
                sys.exit(1)

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
            console.print(f"[bold red]Error:[/bold red] {str(e)}", style="bold red")
            sys.exit(1)

    async def run(self) -> None:
        """Run the interactive CLI loop."""
        self._display_welcome()

        while True:
            try:
                mode = self.mode_manager.current_mode
                mode_indicator = f"[{mode.color}]{mode.name[0]}[/{mode.color}]"
                user_input = console.input(f"{mode_indicator} [bold cyan]You:[/bold cyan] ").strip()
                user_message = InputParser.parse_for_message_type(user_input)
                await user_message.execute(self)
            except KeyboardInterrupt:
                console.print("\n[dim]Exiting due to keyboard interruption[/dim]")
                break
            except EOFError:
                console.print("\n[dim]Exiting due to end of file error[/dim]")
                break

    def _display_welcome(self) -> None:
        """Display welcome message."""
        current_mode = self.mode_manager.current_mode
        welcome_text = """
# Kerkov Bot

Security-focused conversational AI assistant for penetration testing and security analysis.

**Current Mode:** {mode_name} - {mode_desc}

**Commands:**
- Type your message and press Enter
- `exit`, `quit`, or `/q` to exit
- `clear` to clear conversation history
- `/mode` to show current mode
- `/mode [defense|audit|offensive|ctf]` to switch modes
- `/ctf-info` to view/update CTF challenge information (CTF mode only)
- `/shell` to enter or start a shared shell between user and kerkov
- `/model [name]` to switch LLM models
- `/models` to list available models for the current provider
- `/provider [name]` to switch LLM providers
- `/providers` to list supported providers

**Model:** {model}
        """.format(
            mode_name=current_mode.name,
            mode_desc=current_mode.description,
            model=self.provder_manager.provider.model if self.provder_manager.provider else "N/A",
        )

        console.print(Panel(Markdown(welcome_text), border_style=current_mode.color))
        console.print()    

    def clear_history(self) -> None:
        """Clear conversation history."""
        self.tool_approval_helper.clear_optional_approvals()
        self.conversation_history = []
        console.print("[dim]Conversation history cleared[/dim]")
        console.print()    

    async def get_response(self, user_message: Optional[str] = None) -> None:
        """
        Get response from LLM provider with tool execution loop.

        Args:
            user_message: User's input message
        """

        self._apply_system_prompt_if_possible()

        if user_message:
            self.conversation_history.append(Message(role="user", content=user_message))

        try:
            # Tool execution loop - keep calling until no more tool calls
            # Max iterations prevents infinite loops while allowing legitimate multi-step operations
            max_iterations = 10
            iteration = 0

            while iteration < max_iterations:
                iteration += 1

                with console.status("[bold cyan]Thinking...", spinner="dots"):
                    response = await self.provder_manager.provider.complete(
                        messages=self.conversation_history,
                        temperature=0.2,
                        max_tokens=self.config.max_context_tokens,
                        tools=self.tools,
                    )

                if response.tool_calls:
                    tool_names = [tc.tool_name for tc in response.tool_calls]
                    console.print(f"[dim]→ Executing {len(response.tool_calls)} tool(s): {', '.join(tool_names)}[/dim]")

                    assistant_msg = f"I'll use the {', '.join(tool_names)} tool(s) to help with your request."
                    self.conversation_history.append(Message(role="assistant", content=assistant_msg))

                    for tool_call in response.tool_calls:
                        console.print(f"[dim]→ Executing: {tool_call.tool_name}({tool_call.parameters})[/dim]")

                        tool = self._get_tool(tool_call.tool_name)
                        if not tool:
                            console.print(f"[red]Tool '{tool_call.tool_name}' not found[/red]")
                            continue

                        approval_result = self.tool_approval_helper.get_tool_usage_approval(tool, tool_call.parameters)
                        if not approval_result.approved:
                            denial_msg = "User denied the tool call."
                            if approval_result.user_message:
                                denial_msg += f" User said: {approval_result.user_message}"
                            self.conversation_history.append(Message(role="system", content=denial_msg))
                            break

                        with console.status(f"[bold yellow]Running {tool_call.tool_name}...", spinner="dots"):
                            tool_result = await tool.execute(tool_call.parameters)

                        if tool_result.success:
                            console.print(f"[dim green]✓ Command executed[/dim green]")
                            if tool.name == "persistent_shell_execute_cmd" and tool_result.metadata["output_events_only"]:
                                output_events_only = tool_result.metadata["output_events_only"]
                                trimmed = output_events_only[:1000]
                                suffix = "..." if len(output_events_only) > 1000 else ""
                                console.print(f"[dim]{trimmed}{suffix}[/dim]")
                            else:
                                if tool_result.output:
                                    console.print(f"[dim]{tool_result.output[:1000]}{'...' if len(tool_result.output) > 1000 else ''}[/dim]")
                        else:
                            console.print(f"[dim red]✗ Error: {tool_result.error}[/dim red]")

                        result_content = self._format_tool_result(tool_call.tool_name, tool_result)
                        self.conversation_history.append(
                            Message(
                                role="tool",
                                content=result_content,
                            )
                        )

                    continue

                if response.content:
                    self.conversation_history.append(Message(role="assistant", content=response.content))

                    console.print()
                    console.print(Markdown(response.content))
                    console.print()

                    if response.usage:
                        usage_text = (
                            f"[dim]Tokens: {response.usage.prompt_tokens} in, "
                            f"{response.usage.completion_tokens} out, "
                            f"{response.usage.total_tokens} total[/dim]"
                        )
                        console.print(usage_text)
                        console.print()

                break
        
            if iteration >= max_iterations:
                console.print(
                    f"[dim yellow]⚠ Reached maximum iterations ({max_iterations}). "
                    "Stopping to prevent infinite loops.[/dim yellow]"
                )

        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}", style="bold red")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")

    def _apply_system_prompt_if_possible(self):
        """
        Applies system prompt if this is the  

        Returns:
            List of messages ready for LLM
        """
        if len(self.conversation_history) == 0:
            system_prompt = self.mode_manager.current_mode.system_prompt

            if self.mode_manager.current_mode_type == ModeType.CTF and self.ctf_context.has_context():
                system_prompt = system_prompt + "\n" + self.ctf_context.to_prompt_string()

            self.conversation_history.append(Message(role="system", content=system_prompt))

    def _get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """
        Find a tool by name.

        Args:
            tool_name: Name of the tool to find

        Returns:
            BaseTool instance or None if not found
        """
        return next((t for t in self.tools if t.name == tool_name), None)

    def _format_tool_result(self, tool_name: str, tool_result: ToolResult) -> str:
        """
        Format a tool result for adding to conversation.

        Args:
            tool_name: Name of the tool that was executed
            tool_result: ToolResult object

        Returns:
            Formatted string representation
        """
        status = "Success" if tool_result.success else "Failed"
        result_parts = [
            f"Tool '{tool_name}' execution result:",
            f"Status: {status}",
            f"Output:\n{tool_result.output}",
        ]

        if tool_result.error:
            result_parts.append(f"Error: {tool_result.error}")

        if tool_result.metadata:
            result_parts.append(f"Metadata: {tool_result.metadata}")

        return "\n".join(result_parts)
    
    def prompt_ctf_context(self) -> None:
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

        provide_info = console.input("[cyan]Would you like to provide challenge information? (y/n):[/cyan] ").strip().lower()

        if provide_info not in ['y', 'yes']:
            console.print("[dim]Skipping challenge information. Use /ctf-info to add it later.[/dim]")
            console.print()
            return

        self.ctf_context.clear()

        console.print()
        challenge_name = console.input("[cyan]Challenge Name:[/cyan] ").strip()
        if challenge_name:
            self.ctf_context.challenge_name = challenge_name

        category = console.input("[cyan]Category (web exploitation/crypto/binary exploitation/forensics/reverse engineering/misc):[/cyan] ").strip()
        if category:
            self.ctf_context.category = category

        target = console.input("[cyan]Target (URI/file path/IP):[/cyan] ").strip()
        if target:
            self.ctf_context.target = target

        console.print("[cyan]Description (press Enter twice when done):[/cyan]")
        description_lines = []
        while True:
            line = console.input()
            if not line:
                break
            description_lines.append(line)

        if description_lines:
            self.ctf_context.description = "\n".join(description_lines)

        console.print("[cyan]Additional Notes (press Enter twice when done):[/cyan]")
        notes_lines = []
        while True:
            line = console.input()
            if not line:
                break
            notes_lines.append(line)

        if notes_lines:
            self.ctf_context.additional_notes = "\n".join(notes_lines)

        gen_writeup = console.input("[cyan]Would you like for kerkov to generate a CTF writeup? (y/n):[/cyan] ").strip().lower()

        if gen_writeup in ['y', 'yes']:
            console.print("[cyan]The name of the writeup will be the challenge name or an apt generated title if the challenge name contains illegal characters.[/cyan]")
            console.print("[cyan]Specify the directory for the markdown writeup to be generated in. (Press 'enter' to use current dir)[/cyan]")
            dir = console.input("[cyan]Directory:[/cyan] ").strip()
            self.ctf_context.write_up_dir = dir if dir else "."
            
        console.print()
        if self.ctf_context.has_context():
            console.print("[green]✓ Challenge information saved![/green]")
            console.print("[dim]The AI will receive this context with your messages.[/dim]")
        else:
            console.print("[dim]No information provided. You can add it later with /ctf-info[/dim]")
        console.print()
    
    def quit(self):
        sys.exit(0)
