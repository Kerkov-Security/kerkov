from src.cli.messages.user_message import UserMessage


class SkillMessage(UserMessage):
    """Handles /skill commands for activate/add/remove/clear/info."""

    def __init__(self, args: list[str]):
        super().__init__()
        self.args = args

    async def execute(self, cli):
        if not self.args:
            await _show_skill_info(cli)
            return

        command = self.args[0].lower()

        if command in ["off", "clear"]:
            await _clear_skills(cli)
            return

        if command == "add":
            if len(self.args) < 2:
                await _show_usage(cli)
                return
            await _add_skill(cli, self.args[1].lower())
            return

        if command == "remove":
            if len(self.args) < 2:
                await _show_usage(cli)
                return
            await _remove_skill(cli, self.args[1].lower())
            return

        await _activate_single_skill(cli, command)


async def _show_skill_info(cli) -> None:
    console = cli.console
    active_skills = cli.skill_manager.active_skills
    active_names = set([s.name for s in active_skills])

    console.print()
    if active_skills:
        console.print(f"[bold green]Active Skills:[/bold green] {', '.join([s.name for s in active_skills])}")
        allowed = cli.skill_manager.get_allowed_tool_names()
        if allowed is None:
            console.print("[dim]Tool access: all tools[/dim]")
        elif allowed:
            console.print(f"[dim]Tool access: {', '.join(sorted(allowed))}[/dim]")
    else:
        console.print("[dim]No skill active[/dim]")

    skills = cli.skill_manager.list_skills()
    if skills:
        console.print()
        console.print("[bold]Available Skills:[/bold]")
        for skill in sorted(skills.values(), key=lambda s: s.name):
            indicator = "*" if skill.name in active_names else " "
            label = "[dim](built-in)[/dim]" if skill.is_builtin else "[dim](custom)[/dim]"
            tags = f"[dim][{', '.join(skill.tags)}][/dim]" if skill.tags else ""
            console.print(f"  {indicator} [cyan]{skill.name}[/cyan] {tags} - {skill.description} {label}")
    else:
        console.print("[dim]No skills loaded[/dim]")

    await _show_usage(cli)


async def _activate_single_skill(cli, name: str) -> None:
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
    console.print(f"[bold green]Skill set:[/bold green] {skill.name}")
    console.print(f"[dim]{skill.description}[/dim]")
    if skill.tools:
        console.print(f"[dim]Tool access: {', '.join(skill.tools)}[/dim]")
    else:
        console.print("[dim]Tool access: all tools[/dim]")
    console.print()
    console.print("[dim]Applying skill context - clearing conversation history[/dim]")
    console.print()

    cli.clear_history()


async def _add_skill(cli, name: str) -> None:
    console = cli.console
    skill = cli.skill_manager.add_active_skill(name)

    if not skill:
        available = list(cli.skill_manager.list_skills().keys())
        console.print()
        console.print(f"[bold red]Error:[/bold red] Skill '{name}' not found.")
        if available:
            console.print(f"[dim]Available: {', '.join(sorted(available))}[/dim]")
        console.print()
        return

    console.print()
    console.print(f"[bold green]Skill added:[/bold green] {skill.name}")
    console.print("[dim]Applying updated skill context - clearing conversation history[/dim]")
    console.print()
    cli.clear_history()


async def _remove_skill(cli, name: str) -> None:
    console = cli.console
    removed = cli.skill_manager.remove_active_skill(name)
    if not removed:
        console.print()
        console.print(f"[dim]Skill '{name}' is not active[/dim]")
        console.print()
        return

    console.print()
    console.print(f"[bold yellow]Skill removed:[/bold yellow] {name}")
    console.print("[dim]Applying updated skill context - clearing conversation history[/dim]")
    console.print()
    cli.clear_history()


async def _clear_skills(cli) -> None:
    console = cli.console
    active_skills = cli.skill_manager.active_skills

    if len(active_skills) == 0:
        console.print()
        console.print("[dim]No skills are currently active[/dim]")
        console.print()
        return

    active_names = ", ".join([s.name for s in active_skills])
    cli.skill_manager.clear_active_skills()
    console.print()
    console.print(f"[bold yellow]Skills cleared:[/bold yellow] {active_names}")
    console.print("[dim]Clearing conversation history[/dim]")
    console.print()

    cli.clear_history()


async def _show_usage(cli) -> None:
    cli.console.print()
    cli.console.print("[dim]Use `/skill <name>` to activate one skill[/dim]")
    cli.console.print("[dim]Use `/skill add <name>` to stack additional skills[/dim]")
    cli.console.print("[dim]Use `/skill remove <name>` to remove one active skill[/dim]")
    cli.console.print("[dim]Use `/skill clear` (or `/skill off`) to clear all active skills[/dim]")
    cli.console.print()
