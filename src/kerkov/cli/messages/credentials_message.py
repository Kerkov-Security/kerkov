"""CLI credential status, entry, and removal commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.credentials import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    NVIDIA_API_KEY,
    OPENROUTER_API_KEY,
    TAVILY_API_KEY,
    CredentialSpec,
    credential_spec_from_name,
    credential_storage_description,
)
from kerkov.providers.codex.auth import codex_auth_state_is_configured

DISPLAY_CREDENTIALS = [
    ("anthropic", ANTHROPIC_API_KEY),
    ("gemini", GEMINI_API_KEY),
    ("nvidia", NVIDIA_API_KEY),
    ("openrouter", OPENROUTER_API_KEY),
    ("tavily", TAVILY_API_KEY),
]


def canonical_credential_target(spec: CredentialSpec, fallback: str) -> str:
    """Return the provider/tool name used for active-provider comparisons."""

    for target, known_spec in DISPLAY_CREDENTIALS:
        if spec == known_spec:
            return target
    return fallback


async def disconnect_active_provider(cli: Any, target: str) -> bool:
    """Disconnect an active provider after its local credential is removed."""

    if cli.current_provider_name() != target:
        return False
    cli.provder_manager.clear_provider()
    await cli._reconfigure_context_for_provider()
    return True


class CredentialsMessage(UserMessage):
    """Manage credentials without accepting secret values in command text."""

    def __init__(
        self,
        action: str = "status",
        target: str = "",
        *,
        session_only: bool = False,
    ) -> None:
        self.action = action
        self.target = target
        self.session_only = session_only

    async def execute(self, cli) -> None:
        if self.action == "status":
            self._show_status(cli)
            return
        if self.action not in {"set", "remove"}:
            cli.console.print(
                "[bold red]Error:[/bold red] Use `/auth`, "
                "`/auth set <name> [--session]`, or `/auth remove <name>`. "
                "Never place an API key directly in the command."
            )
            cli.console.print()
            return

        spec = credential_spec_from_name(self.target)
        if spec is None:
            cli.console.print(
                f"[bold red]Error:[/bold red] Unknown credential target "
                f"'{self.target}'. Use anthropic, gemini, nvidia, openrouter, "
                "tavily, or an uppercase profile variable such as HTB_API_TOKEN."
            )
            cli.console.print()
            return

        if self.action == "remove":
            warning = cli.credentials.delete(spec)
            target = canonical_credential_target(
                spec,
                self.target.strip().lower(),
            )
            disconnected = await disconnect_active_provider(cli, target)
            cli.console.print(f"[green]✓[/green] Cleared available {spec.label}.")
            if warning:
                cli.console.print(f"[yellow]{warning}[/yellow]")
            if disconnected:
                cli.console.print(
                    "[dim]The active provider was disconnected. Run /provider to "
                    "choose another provider or sign in again.[/dim]"
                )
            cli.console.print()
            return

        credential_input = await cli.prompt_for_credential(
            spec,
            persist=not self.session_only,
        )
        if credential_input is None:
            return

        result = cli.credentials.save(
            spec,
            credential_input.value,
            persist=credential_input.persist,
        )
        if result.warning:
            cli.console.print(f"[yellow]{result.warning}[/yellow]")
        elif result.persisted:
            cli.console.print(
                f"[green]✓[/green] {spec.label} saved in the "
                f"{credential_storage_description(result.storage)}."
            )
        else:
            cli.console.print(f"[green]✓[/green] {spec.label} stored for this session.")
        cli.console.print()

    @staticmethod
    def _show_status(cli) -> None:
        cli.console.print()
        cli.console.print("[bold]Credentials:[/bold]")
        for name, spec in DISPLAY_CREDENTIALS:
            source = cli.credentials.source(spec)
            status = f"configured ({source})" if source else "not configured"
            cli.console.print(f"  - {name}: {status}")
        codex_state_path = getattr(
            getattr(cli, "config", None),
            "codex_state_path",
            None,
        )
        codex_configured = bool(
            codex_state_path and codex_auth_state_is_configured(Path(codex_state_path))
        )
        codex_status = (
            "configured (browser sign-in)" if codex_configured else "not configured"
        )
        cli.console.print(f"  - codex: {codex_status}")
        cli.console.print()
