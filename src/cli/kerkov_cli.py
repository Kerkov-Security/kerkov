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

from src.core.config_manager import get_config
from src.models.basic_models import Message
from src.providers.gemini_provider import GeminiProvider
from src.tools.base_tool import BaseTool
from src.tools.shell_tool import ShellTool

console = Console()


class KerkovCLI:
    """Interactive CLI for Kerkov Bot."""

    def __init__(self):
        """Initialize the CLI."""
        self.config = get_config()
        self.conversation_history: List[Message] = []
        self.provider = None
        self.tools = [ShellTool()]
        self.system_prompt = (
            "You are Kerkov, a security-focused AI assistant specializing in "
            "penetration testing and security analysis. You have access to shell "
            "commands to help analyze systems. When the user asks you to perform "
            "tasks that require shell commands, use the shell tool. Always explain "
            "what you're doing before and after executing commands."
        )
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

        while True:
            try:
                user_input = console.input("[bold cyan]You:[/bold cyan] ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ["exit", "quit", "/q"]:
                    console.print("[dim]Goodbye![/dim]")
                    break

                if user_input.lower() == "clear":
                    self._clear_history()
                    continue

                await self._get_response(user_input)

            except KeyboardInterrupt:
                console.print("\n[dim]Goodbye![/dim]")
                break
            except EOFError:
                console.print("\n[dim]Goodbye![/dim]")
                break

    def _display_welcome(self) -> None:
        """Display welcome message."""
        welcome_text = """
# Kerkov Bot

Security-focused conversational AI assistant for penetration testing and security analysis.

**Commands:**
- Type your message and press Enter
- `exit`, `quit`, or `/q` to exit
- `clear` to clear conversation history

**Model:** {model}
        """.format(
            model=self.config.default_model,
        )

        console.print(Panel(Markdown(welcome_text), border_style="blue"))
        console.print()    

    def _clear_history(self) -> None:
        """Clear conversation history."""
        self.conversation_history = []
        console.print("[dim]Conversation history cleared[/dim]")
        console.print()    

    async def _get_response(self, user_message: str) -> None:
        """
        Get response from LLM provider with tool execution loop.

        Args:
            user_message: User's input message
        """
        self.conversation_history.append(Message(role="user", content=user_message))

        messages_to_send = self._prepare_messages()

        try:
            # Tool execution loop - keep calling until no more tool calls
            max_iterations = 5
            iteration = 0

            while iteration < max_iterations:
                iteration += 1

                with console.status("[bold cyan]Thinking...", spinner="dots"):
                    response = await self.provider.complete(
                        messages=messages_to_send,
                        temperature=0.2,
                        max_tokens=self.config.max_context_tokens,
                        tools=self.tools,
                    )

                # Check if LLM wants to use a tool
                if response.tool_calls:
                    # First, add the assistant's message indicating it wants to use tools
                    # This is important so Gemini knows it already made this decision
                    tool_names = [tc.tool_name for tc in response.tool_calls]
                    assistant_msg = f"I'll use the {', '.join(tool_names)} tool(s)."
                    messages_to_send.append(Message(role="assistant", content=assistant_msg))

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

                        # Add tool result to messages for next iteration
                        result_content = self._format_tool_result(tool_call.tool_name, tool_result)
                        messages_to_send.append(Message(role="user", content=result_content))

                    # Continue loop to get final response
                    continue

                # No tool calls - this is the final response
                if response.content:
                    self.conversation_history.append(
                        Message(role="assistant", content=response.content)
                    )

                    console.print()
                    console.print(Markdown(response.content))
                    console.print()

                    # Display token usage if available
                    if response.usage:
                        usage_text = (
                            f"[dim]Tokens: {response.usage.prompt_tokens} in, "
                            f"{response.usage.completion_tokens} out, "
                            f"{response.usage.total_tokens} total[/dim]"
                        )
                        console.print(usage_text)
                        console.print()

                break

        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}", style="bold red")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")


    def _prepare_messages(self) -> List[Message]:
        """
        Prepare messages for sending to LLM, including system prompt if needed.

        Returns:
            List of messages ready for LLM
        """
        messages = self.conversation_history.copy()

        # Add system prompt only on first message
        if self.system_prompt and len(self.conversation_history) == 1:
            messages.insert(0, Message(role="system", content=self.system_prompt))

        return messages

    def _get_tool(self, tool_name: str) -> Optional[BaseTool]:
        """
        Find a tool by name.

        Args:
            tool_name: Name of the tool to find

        Returns:
            BaseTool instance or None if not found
        """
        return next((t for t in self.tools if t.name == tool_name), None)

    def _format_tool_result(self, tool_name: str, tool_result) -> str:
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

        return "\n".join(result_parts)
