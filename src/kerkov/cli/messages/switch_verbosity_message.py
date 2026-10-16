from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.session.verbosity_manager import (
    VERBOSITY_COLORS,
    VERBOSITY_DESCRIPTIONS,
    VerbosityManager,
)
from kerkov.core.session.verbosity_prompts import apply_verbosity_to_system_prompt


class SwitchVerbosityMessage(UserMessage):
    """Switch verbosity (narration level) without changing approval mode."""

    def __init__(self, verbosity_str: str):
        super().__init__()
        self.verbosity_str = verbosity_str

    async def execute(self, cli):
        console = cli.console
        manager = cli.verbosity_manager

        parsed = VerbosityManager.parse(self.verbosity_str)
        if parsed is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid verbosity '{self.verbosity_str}'",
                style="bold red",
            )
            console.print(
                "[dim]Valid verbosities: guided, standard, aggressive, silent[/dim]"
            )
            console.print()
            return

        old = manager.current
        new = manager.set(parsed)

        color = VERBOSITY_COLORS[new]
        console.print()
        console.print(
            f"[bold {color}]Verbosity switched:[/bold {color}] "
            f"{old.value.capitalize()} → {new.value.capitalize()}"
        )
        console.print(f"[dim]{VERBOSITY_DESCRIPTIONS[new]}[/dim]")
        console.print()

        apply_verbosity_to_system_prompt(cli.conversation_history, new)
        cli.engagement_store.update_verbosity(new.value)
