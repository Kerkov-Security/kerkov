"""Interactive local credential removal."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from kerkov.cli.messages.credentials_message import DISPLAY_CREDENTIALS
from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.credentials import CredentialSpec, credential_spec_from_name
from kerkov.providers.codex.auth import (
    clear_codex_auth_state,
    codex_auth_state_is_configured,
)

CREDENTIAL_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "nvidia": "NVIDIA",
    "openrouter": "OpenRouter",
    "tavily": "Tavily",
    "codex": "Codex",
}


class LogoutMessage(UserMessage):
    """Choose and remove a locally available credential after confirmation."""

    def __init__(self, target: str = "") -> None:
        self.target = target.strip()

    async def execute(self, cli) -> None:
        target = self.target.lower()
        spec: Optional[CredentialSpec] = None
        source: Optional[str] = None

        if target:
            if target in {"codex", "openai-codex"}:
                target = "codex"
                source = self._codex_source(cli)
            else:
                spec = credential_spec_from_name(self.target)
                if spec is None:
                    self._print_unknown_target(cli)
                    return
                target = self._canonical_target(spec, target)
                source = cli.credentials.source(spec)
        else:
            selected = await self._choose_credential(cli)
            if selected is None:
                return
            target = selected
            if target == "codex":
                source = self._codex_source(cli)
            else:
                spec = credential_spec_from_name(target)
                if spec is not None:
                    source = cli.credentials.source(spec)

        if not source:
            label = self._display_name(target, spec)
            cli.console.print()
            cli.console.print(
                f"[yellow]{label} has no locally available credential.[/yellow]"
            )
            cli.console.print()
            return

        label = self._display_name(target, spec)
        confirmed = await cli.input_manager.choose(
            f"Remove local {label} credential?",
            [
                ("remove", f"Remove {label} credential"),
                ("cancel", "Cancel"),
            ],
            default="cancel",
        )
        if confirmed != "remove":
            return

        try:
            warning = self._remove_credential(cli, target, spec)
        except OSError as exc:
            cli.console.print()
            cli.console.print(
                f"[bold red]Could not remove {label} credential:[/bold red] {exc}"
            )
            cli.console.print()
            return

        disconnected = await self._disconnect_active_provider(cli, target)
        cli.console.print()
        cli.console.print(f"[green]✓[/green] Cleared managed local {label} credential.")
        if warning:
            cli.console.print(f"[yellow]{warning}[/yellow]")
        if disconnected:
            cli.console.print(
                "[dim]The active provider was disconnected. Run /provider to "
                "choose another provider or sign in again.[/dim]"
            )
        cli.console.print()

    async def _choose_credential(self, cli) -> Optional[str]:
        options: list[tuple[str, str]] = []
        for target, spec in DISPLAY_CREDENTIALS:
            source = cli.credentials.source(spec)
            if source:
                options.append(
                    (
                        target,
                        f"{CREDENTIAL_DISPLAY_NAMES[target]} — {source}",
                    )
                )

        codex_source = self._codex_source(cli)
        if codex_source:
            options.append(("codex", f"Codex — {codex_source}"))

        if not options:
            cli.console.print()
            cli.console.print("[dim]No locally available credentials to remove.[/dim]")
            cli.console.print(
                "[dim]Credentials supplied by custom HTTP profiles can be removed "
                "with /auth remove <PROFILE_ENV_VAR>.[/dim]"
            )
            cli.console.print()
            return None

        return await cli.input_manager.choose(
            "Select a credential to remove",
            options,
        )

    @staticmethod
    def _codex_state_path(cli) -> Optional[Path]:
        configured = getattr(getattr(cli, "config", None), "codex_state_path", None)
        return Path(configured) if configured else None

    @classmethod
    def _codex_source(cls, cli) -> Optional[str]:
        state_path = cls._codex_state_path(cli)
        if state_path is None or not codex_auth_state_is_configured(state_path):
            return None
        return "browser sign-in"

    @classmethod
    def _remove_credential(
        cls,
        cli,
        target: str,
        spec: Optional[CredentialSpec],
    ) -> Optional[str]:
        if target == "codex":
            state_path = cls._codex_state_path(cli)
            if state_path is not None:
                clear_codex_auth_state(state_path)
            return None

        assert spec is not None
        return cli.credentials.delete(spec)

    @staticmethod
    async def _disconnect_active_provider(cli, target: str) -> bool:
        current_provider = cli.current_provider_name()
        if current_provider != target:
            return False
        cli.provder_manager.clear_provider()
        await cli._reconfigure_context_for_provider()
        return True

    @staticmethod
    def _display_name(target: str, spec: Optional[CredentialSpec]) -> str:
        if target in CREDENTIAL_DISPLAY_NAMES:
            return CREDENTIAL_DISPLAY_NAMES[target]
        if spec is not None:
            return spec.label
        return target

    @staticmethod
    def _canonical_target(spec: CredentialSpec, fallback: str) -> str:
        for target, known_spec in DISPLAY_CREDENTIALS:
            if spec == known_spec:
                return target
        return fallback

    @staticmethod
    def _print_unknown_target(cli) -> None:
        cli.console.print()
        cli.console.print(
            "[bold red]Unknown credential target.[/bold red] Use anthropic, "
            "gemini, nvidia, openrouter, tavily, codex, or an uppercase profile "
            "variable such as HTB_API_TOKEN."
        )
        cli.console.print()
