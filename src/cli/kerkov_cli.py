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

from src.cli.input_handler import InputHandler, InputType
from src.cli.mode_manager import ModeManager
from src.cli.modes import ModeType
from src.cli.ctf_context import CTFContext
from src.core.config_manager import get_config
from src.models.basic_models import Message
from src.providers.gemini_provider import GeminiProvider
from src.tools.base_tool import BaseTool, ToolResult
from src.tools.shell_tool import ShellTool
from src.tools.read_tool import ReadTool
from src.tools.write_tool import WriteTool
from src.tools.fetch_tool import FetchTool

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
        self.provider = None
        self.tools = [ShellTool(), ReadTool(), WriteTool(), FetchTool()]
        self.mode_manager = ModeManager(initial_mode=mode)
        self.ctf_context = CTFContext()
        self._initialize_provider()

    def _initialize_provider(self) -> None:
        """Initialize the LLM provider based on configuration."""
        if not self.config.google_api_key:
            console.print(
                "[bold red]Error:[/bold red] GOOGLE_API_KEY not found in .env file",
                style="bold red",
            )
            console.print("Please add your Google API key to the .env file")
            sys.exit(1)

        try:
            self.provider = GeminiProvider(
                api_key=self.config.google_api_key,
                model=self.config.default_model,
            )

            if not self.provider.validate_connection():
                console.print(
                    "[bold red]Error:[/bold red] Failed to connect to Gemini API",
                    style="bold red",
                )
                console.print("Please check your API key and internet connection")
                sys.exit(1)

            console.print(
                f"[green]✓[/green] Connected to {self.config.default_model}",
                style="dim",
            )

        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}", style="bold red")
            sys.exit(1)     

    async def run(self) -> None:
        """Run the interactive CLI loop."""
        self._display_welcome()

        input_handler = InputHandler(
            console=console,
            mode_manager=self.mode_manager,
            ctf_context=self.ctf_context,
        )

        while True:
            try:
                mode = self.mode_manager.current_mode
                mode_indicator = f"[{mode.color}]{mode.name[0]}[/{mode.color}]"
                user_input = console.input(f"{mode_indicator} [bold cyan]You:[/bold cyan] ").strip()

                input_type = input_handler.handleUserInput(user_input)

                if input_type == InputType.UNKNOWN:
                    continue
                elif input_type == InputType.QUIT:
                    break
                elif input_type == InputType.CLEAR:
                    self._clear_history()
                    continue
                elif input_type == InputType.INFO:
                    continue
                elif input_type == InputType.CTF_MODE_SWITCH:
                    self._clear_history()
                    if self.ctf_context.has_context():
                        await self._get_response()
                elif input_type == InputType.MODE_SWITCH:
                    self._clear_history()
                    console
                    continue

                await self._get_response(user_input)

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

**Model:** {model}
        """.format(
            mode_name=current_mode.name,
            mode_desc=current_mode.description,
            model=self.config.default_model,
        )

        console.print(Panel(Markdown(welcome_text), border_style=current_mode.color))
        console.print()    

    def _clear_history(self) -> None:
        """Clear conversation history."""
        self.conversation_history = []
        console.print("[dim]Conversation history cleared[/dim]")
        console.print()    

    async def _get_response(self, user_message: str|None = None) -> None:
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
                    response = await self.provider.complete(
                        messages=self.conversation_history,
                        temperature=0.2,
                        max_tokens=self.config.max_context_tokens,
                        tools=self.tools,
                    )

                if response.tool_calls:
                    tool_names = [tc.tool_name for tc in response.tool_calls]
                    console.print(f"[dim]→ Executing {len(response.tool_calls)} tool(s): {', '.join(tool_names)}[/dim]")

                    print(f"Tool calls: {response.tool_calls}")

                    # Add assistant message to conversation history
                    assistant_msg = f"I'll use the {', '.join(tool_names)} tool(s) to help with your request."
                    self.conversation_history.append(Message(role="assistant", content=assistant_msg))

                    for tool_call in response.tool_calls:
                        console.print(f"[dim]→ Executing: {tool_call.tool_name}({tool_call.parameters})[/dim]")

                        tool = self._get_tool(tool_call.tool_name)
                        if not tool:
                            console.print(f"[red]Tool '{tool_call.tool_name}' not found[/red]")
                            continue

                        with console.status(f"[bold yellow]Running {tool_call.tool_name}...", spinner="dots"):
                            tool_result = await tool.execute(**tool_call.parameters)

                        if tool_result.success:
                            console.print(f"[dim green]✓ Command executed[/dim green]")
                            if tool_result.output:
                                console.print(f"[dim]{tool_result.output[:500]}{'...' if len(tool_result.output) > 500 else ''}[/dim]")
                        else:
                            console.print(f"[dim red]✗ Error: {tool_result.error}[/dim red]")

                        result_content = self._format_tool_result(tool_call.tool_name, tool_result)
                        self.conversation_history.append(Message(role="user", content=result_content))

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
