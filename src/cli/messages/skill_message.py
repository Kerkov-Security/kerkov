from src.cli.messages.user_message import UserMessage


class SkillMessage(UserMessage):
    """Handles /skill [name|off] — activate, deactivate, or show skill info."""

    def __init__(self, args: list[str]):
        super().__init__()
        self.args = args

    async def execute(self, cli):
        if not self.args:
            await _show_skill_info(cli)
            return

        target = self.args[0].lower()

        if target == "off":
            await _deactivate_skill(cli)
        else:
            await _activate_skill(cli, target)


async def _show_skill_info(cli) -> None:
    console = cli.console
    active = cli.skill_manager.active_skill

    console.print()
    if active:
        console.print(f"[bold green]Active Skill:[/bold green] {active.name}")
        console.print(f"[dim]{active.description}[/dim]")
        if active.tools:
            console.print(f"[dim]Restricted tools: {', '.join(active.tools)}[/dim]")
    else:
        console.print("[dim]No skill active[/dim]")

    skills = cli.skill_manager.list_skills()
    if skills:
        console.print()
        console.print("[bold]Available Skills:[/bold]")
        for skill in sorted(skills.values(), key=lambda s: s.name):
            indicator = "→" if active and skill.name == active.name else " "
            label = "[dim](built-in)[/dim]" if skill.is_builtin else "[dim](custom)[/dim]"
            tags = f"[dim][{', '.join(skill.tags)}][/dim]" if skill.tags else ""
            console.print(f"  {indicator} [cyan]{skill.name}[/cyan] {tags} — {skill.description} {label}")
    else:
        console.print("[dim]No skills loaded[/dim]")

    console.print()
    console.print("[dim]Use `/skill <name>` to activate or `/skill off` to deactivate[/dim]")
    console.print()


async def _activate_skill(cli, name: str) -> None:
    console = cli.console
    skill = cli.skill_manager.activate(name)

    if not skill:
        available = list(cli.skill_manager.list_skills().keys())
        console.print()
        console.print(f"[bold red]Error:[/bold red] Skill '{name}' not found.")
        if available:
            console.print(f"[dim]Available: {', '.join(sorted(available))}[/dim]")
        console.print()
        return

    console.print()
    console.print(f"[bold green]Skill activated:[/bold green] {skill.name}")
    console.print(f"[dim]{skill.description}[/dim]")
    if skill.tools:
        console.print(f"[dim]Tool access restricted to: {', '.join(skill.tools)}[/dim]")
    console.print()
    console.print("[dim]Applying skill context — clearing conversation history[/dim]")
    console.print()

    cli.clear_history()


async def _deactivate_skill(cli) -> None:
    console = cli.console
    active = cli.skill_manager.active_skill

    if not active:
        console.print()
        console.print("[dim]No skill is currently active[/dim]")
        console.print()
        return

    cli.skill_manager.deactivate()
    console.print()
    console.print(f"[bold yellow]Skill deactivated:[/bold yellow] {active.name}")
    console.print("[dim]Clearing conversation history[/dim]")
    console.print()

    cli.clear_history()
