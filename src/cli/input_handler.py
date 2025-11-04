from dataclasses import dataclass
from enum import Enum
from typing import Optional
from rich.console import Console
from rich.panel import Panel

from src.cli.mode_manager import ModeManager
from src.cli.ctf_context import CTFContext
from src.cli.modes import ModeType

class InputType(Enum):
    UNKNOWN = "unknown"
    QUIT = "quit"
    CLEAR = "clear"
    INFO = "info"
    CTF_MODE_SWITCH = "ctf_mode_switch"
    MODE_SWITCH = "mode_switch"
    CTF_INFO = "ctf_info"
    GENERAL = "general"

class InputHandler:

    def __init__(
        self,
        console: Console,
        mode_manager: ModeManager,
        ctf_context: Optional[CTFContext] = None,
    ):
        self.console = console
        self.mode_manager = mode_manager
        self.ctf_context = ctf_context or CTFContext()

    def handleUserInput(self, user_input: str) -> InputType:
        if not user_input:
            return InputType.UNKNOWN
    
        if user_input.lower() in ["exit", "quit", "/q"]:
            return InputType.QUIT

        if user_input.lower() == "clear":
            return InputType.CLEAR

        if user_input.lower().startswith("/mode"):
            return self._handle_mode_command(user_input)

        if user_input.lower().startswith("/ctf-info"):
            self._handle_ctf_info_command()
            return InputType.CTF_INFO

        return InputType.GENERAL
        
    def _handle_mode_command(self, user_input: str) -> InputType:
        """
        Handle /mode command for displaying or switching modes.

        Args:
            user_input: The user's input starting with /mode
        """
        parts = user_input.strip().split()

        console = self.console
        if len(parts) == 1:
            current_mode = self.mode_manager.current_mode
            console.print()
            console.print(
                f"[bold {current_mode.color}]Current Mode:[/bold {current_mode.color}] {current_mode.name}"
            )
            console.print(f"[dim]{current_mode.description}[/dim]")
            console.print()
            console.print("[bold]Available Modes:[/bold]")

            for mode_type, mode in self.mode_manager.list_modes().items():
                indicator = "→" if mode_type == self.mode_manager.current_mode_type else " "
                console.print(
                    f"  {indicator} [{mode.color}]{mode.name}[/{mode.color}]: {mode.description}"
                )

            console.print()
            console.print("[dim]Use `/mode [defense|audit|offensive|ctf]` to switch modes[/dim]")
            console.print()
            return InputType.INFO

        # /mode <mode_name> - switch to specified mode
        mode_str = parts[1]
        new_mode_type = ModeManager.parse_mode_string(mode_str)

        if new_mode_type is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid mode '{mode_str}'",
                style="bold red",
            )
            console.print(
                "[dim]Valid modes: defense, audit, offensive, ctf[/dim]"
            )
            console.print()
            return InputType.INFO
        
        old_mode = self.mode_manager.current_mode
        new_mode = self.mode_manager.switch_mode(new_mode_type)

        console.print()
        console.print(
            f"[bold {new_mode.color}]Mode switched:[/bold {new_mode.color}] "
            f"{old_mode.name} → {new_mode.name}"
        )
        console.print(f"[dim]{new_mode.description}[/dim]")

        if new_mode_type == ModeType.CTF:
            self._prompt_ctf_context()
            return InputType.CTF_MODE_SWITCH
        
        console.print()
        console.print("[dim]Applying new system prompt[/dim]")
        console.print()
        
        return InputType.MODE_SWITCH

    def _prompt_ctf_context(self) -> None:
        """Prompt user for optional CTF challenge information."""
        console = self.console

        console.print(
            Panel(
                "[bold cyan]CTF Challenge Information[/bold cyan]\n\n"
                "Provide optional context about the CTF challenge.\n"
                "Press Enter to skip any field, or type 'skip' to skip all.\n"
                "You can update this later with [bold]/ctf-info[/bold]",
                border_style="cyan"
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

        gen_writeup = console.input("[cyan]Would for kerkov to generate a CTF writeup? (y/n):[/cyan] ").strip().lower()

        if gen_writeup in ['y', 'yes']:
            console.print("[cyan]The name of the writeup will be the challenge name or an apt generated title if the challenge name contains illegal characters.[/cyan]")
            console.print("[cyan]Specify the directory for the markdown writeup to be generated in. (Press 'enter' to use current dir)[/cyan]")
            dir = console.input("[cyan]Directory:[/cyan] ").strip()
            if dir:
                self.ctf_context.write_up_dir = dir
            
        console.print()
        if self.ctf_context.has_context():
            console.print("[green]✓ Challenge information saved![/green]")
            console.print("[dim]The AI will receive this context with your messages.[/dim]")
        else:
            console.print("[dim]No information provided. You can add it later with /ctf-info[/dim]")
        console.print()

    def _handle_ctf_info_command(self) -> None:
        """Handle /ctf-info command to view or update CTF challenge information."""
        console = self.console

        if self.mode_manager.current_mode_type != ModeType.CTF:
            console.print()
            console.print("[yellow]⚠ CTF challenge information is only available in CTF mode[/yellow]")
            console.print("[dim]Switch to CTF mode with: /mode ctf[/dim]")
            console.print()
            return

        if self.ctf_context.has_context():
            console.print()
            console.print(Panel(
                f"[bold cyan]Current CTF Challenge Information[/bold cyan]\n\n"
                f"{self._format_ctf_context_display()}",
                border_style="cyan"
            ))
            console.print()

            update = console.input("[cyan]Update challenge information? (y/n):[/cyan] ").strip().lower()
            if update in ['y', 'yes']:
                self._prompt_ctf_context()
            else:
                clear = console.input("[cyan]Clear challenge information? (y/n):[/cyan] ").strip().lower()
                if clear in ['y', 'yes']:
                    self.ctf_context.clear()
                    console.print("[green]✓ Challenge information cleared[/green]")
                    console.print()
        else:
            console.print()
            console.print("[dim]No CTF challenge information set.[/dim]")
            console.print()
            self._prompt_ctf_context()

    def _format_ctf_context_display(self) -> str:
        """Format CTF context for display to user."""
        parts = []

        if self.ctf_context.challenge_name:
            parts.append(f"[bold]Challenge:[/bold] {self.ctf_context.challenge_name}")

        if self.ctf_context.category:
            parts.append(f"[bold]Category:[/bold] {self.ctf_context.category}")

        if self.ctf_context.target:
            parts.append(f"[bold]Target:[/bold] {self.ctf_context.target}")

        if self.ctf_context.description:
            parts.append(f"\n[bold]Description:[/bold]\n{self.ctf_context.description}")

        if self.ctf_context.additional_notes:
            parts.append(f"\n[bold]Notes:[/bold]\n{self.ctf_context.additional_notes}")

        return "\n".join(parts) if parts else "[dim]No information available[/dim]"
