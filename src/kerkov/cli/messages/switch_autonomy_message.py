from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.session.autonomy_manager import (
    AUTONOMY_COLORS,
    AUTONOMY_DESCRIPTIONS,
    AutonomyManager,
    preset_for,
)
from kerkov.core.session.verbosity_prompts import apply_verbosity_to_system_prompt


class SwitchAutonomyMessage(UserMessage):

    def __init__(self, level_str: str):
        super().__init__()
        self.level_str = level_str

    async def execute(self, cli):
        console = cli.console
        manager = cli.autonomy_manager

        parsed = AutonomyManager.parse_level(self.level_str)
        if parsed is None:
            console.print(
                f"[bold red]Error:[/bold red] Invalid autonomy level '{self.level_str}'",
                style="bold red",
            )
            console.print(
                "[dim]Valid levels: 1-4 or guided, standard, aggressive, autonomous[/dim]"
            )
            console.print()
            return

        try:
            old_level = manager.current_level
            new_level = manager.set_level(parsed.value)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            console.print()
            return

        old_label = "Custom" if old_level is None else old_level.name.capitalize()
        color = AUTONOMY_COLORS[new_level]
        console.print()
        console.print(
            f"[bold {color}]Autonomy switched:[/bold {color}] "
            f"{old_label} → {new_level.name.capitalize()}"
        )
        console.print(f"[dim]{AUTONOMY_DESCRIPTIONS[new_level]}[/dim]")
        console.print()

        # Autonomy only changes *how* the LLM communicates and *whether* tools
        # need approval; the conversation context stays intact.
        new_verbosity, new_approval_mode = preset_for(new_level)
        apply_verbosity_to_system_prompt(cli.conversation_history, new_verbosity)

        cli.engagement_store.update_verbosity(new_verbosity.value)
        cli.engagement_store.update_approval_mode(new_approval_mode.value)
