"""
Skill slash command message types:
/skill, /skill <id>, /skill <id> <args>, /skill search <query>
"""

from rich.table import Table

from kerkov.cli.messages.user_message import UserMessage


class SkillListMessage(UserMessage):
    """Displays all loaded skills in a Rich table."""

    async def execute(self, cli):
        console = cli.console
        skills = cli.skill_manager.list_skills()

        if not skills:
            console.print("[dim]No skills loaded.[/dim]")
            console.print()
            return

        table = Table(title="Available Skills", border_style="cyan")
        table.add_column("Name", style="bold cyan")
        table.add_column("Description")
        table.add_column("Difficulty", style="dim")
        table.add_column("Category", style="dim")
        table.add_column("Status", style="dim")

        for entry in skills:
            difficulty = ""
            category = ""
            if entry.kerkov:
                difficulty = entry.kerkov.difficulty or ""
                category = entry.kerkov.category or ""

            eligibility = cli.skill_manager.check_eligibility(entry)
            if eligibility.eligible:
                status = "[green]ready[/green]"
            else:
                missing = ", ".join(eligibility.missing_bins)
                status = f"[yellow]missing: {missing}[/yellow]"

            table.add_row(entry.name, entry.description, difficulty, category, status)

        console.print()
        console.print(table)
        console.print()
        console.print(
            "[dim]Use /skill <name> [args] to load a skill, /skill search <query> to search[/dim]"
        )
        console.print()


class SkillLoadMessage(UserMessage):
    """Loads a skill and sends its content to the LLM."""

    def __init__(self, skill_id: str, skill_args: str | None = None):
        self.skill_id = skill_id
        self.skill_args = skill_args

    async def execute(self, cli):
        console = cli.console
        entry = cli.skill_manager.get_skill(self.skill_id)

        if not entry:
            console.print(f"[red]Skill '{self.skill_id}' not found.[/red]")
            console.print()
            return

        if not entry.user_invocable:
            console.print(
                f"[yellow]Skill '{self.skill_id}' is not user-invocable.[/yellow]"
            )
            console.print()
            return

        required_credentials = (
            entry.kerkov.requires.credentials
            if entry.kerkov
            and entry.kerkov.requires
            and entry.kerkov.requires.credentials
            else []
        )
        if required_credentials:
            ensured = await cli.credentials.ensure_environment_credentials(
                required_credentials,
                cli.prompt_for_credential,
                context=f"skill '{entry.name}'",
            )
            if not ensured.ready:
                assert ensured.missing is not None
                console.print(
                    f"[yellow]Skill loading cancelled: "
                    f"{ensured.missing.env_var} is required.[/yellow]"
                )
                console.print()
                return
            for _, save_result in ensured.saved:
                if save_result.warning:
                    console.print(f"[yellow]{save_result.warning}[/yellow]")

        eligibility = cli.skill_manager.check_eligibility(entry)
        if not eligibility.eligible:
            missing = ", ".join(eligibility.missing_bins)
            console.print(
                f"[yellow]Warning: Missing required binaries: {missing}[/yellow]"
            )

        loaded_msg = f"[dim]Loaded skill: {entry.name}"
        if self.skill_args:
            loaded_msg += f" | args: {self.skill_args}"
        loaded_msg += "[/dim]"
        console.print(loaded_msg)

        prompt = (
            f"I've loaded the skill '{entry.name}': {entry.description}\n\n"
            f"Skill instructions:\n\n{entry.content}"
        )
        if self.skill_args:
            prompt += (
                "\n\n"
                f"User-provided skill input:\n\n{self.skill_args}\n\n"
                "Apply the skill to that input."
            )
        await cli.get_response(prompt)


class SkillSearchMessage(UserMessage):
    """Searches skills by keyword and displays results."""

    def __init__(self, query: str):
        self.query = query

    async def execute(self, cli):
        console = cli.console
        results = cli.skill_manager.search(query=self.query)

        if not results:
            console.print(f"[dim]No skills matched '{self.query}'[/dim]")
            console.print()
            return

        console.print(f"[bold]Skills matching '{self.query}':[/bold]")
        for entry in results:
            difficulty = ""
            if entry.kerkov and entry.kerkov.difficulty:
                difficulty = f" [{entry.kerkov.difficulty}]"
            console.print(
                f"  [cyan]{entry.name}[/cyan]: {entry.description}{difficulty}"
            )

        console.print()
        console.print("[dim]Use /skill <name> [args] to load a skill[/dim]")
        console.print()
