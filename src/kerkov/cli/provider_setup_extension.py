"""Provider onboarding and runtime credential support for the interactive CLI."""

import asyncio
import contextlib
import os
import sys
from abc import ABC, abstractmethod
from html import escape as escape_html
from typing import Optional
from urllib.parse import urlsplit

from prompt_toolkit.formatted_text import AnyFormattedText
from rich.console import Console
from rich.markup import escape as escape_rich_markup

from kerkov.cli.buffering_sink import BufferingSink
from kerkov.cli.choice_styles import active_choice_label
from kerkov.cli.input_manager import InputManager
from kerkov.core.config_manager import ConfigManager, get_config, save_user_setting
from kerkov.core.credentials import (
    CredentialInput,
    CredentialManager,
    CredentialSpec,
    credential_storage_description,
    provider_credential_spec,
)
from kerkov.core.logging.logger_factory import get_logger
from kerkov.providers import (
    MissingCodexAuthError,
    MissingCredentialError,
    list_providers,
)
from kerkov.providers.codex import run_codex_oauth_browser_login
from kerkov.providers.provider_manager import ProviderManager

logger = get_logger("KerkovCLI")

SETUP_LATER_PROVIDER = "__setup_later__"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
PROVIDER_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "nvidia": "NVIDIA",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "ollama": "Ollama",
    "codex": "Codex",
}


def _normalize_ollama_base_url(value: str) -> str:
    """Validate and normalize an Ollama API origin."""

    entered = value.strip()
    try:
        parsed = urlsplit(entered)
        parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid Ollama server URL.") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Ollama server URL must start with http:// or https://.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama server URL must not contain a username or password.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            "Ollama server URL must be an origin without a path, query, or fragment."
        )

    return parsed._replace(scheme=parsed.scheme.lower(), path="").geturl()


class ProviderSetupExtension(ABC):
    """Add provider selection and credential onboarding to a CLI host."""

    config: ConfigManager
    credentials: CredentialManager
    provder_manager: ProviderManager
    input_manager: InputManager
    console: Console
    output_sink: BufferingSink

    async def prompt_for_credential(
        self,
        spec: CredentialSpec,
        persist: bool = True,
    ) -> Optional[CredentialInput]:
        """Collect a credential outside chat using password-style input."""

        output_sink = getattr(self, "output_sink", None)
        status_suspension = (
            output_sink.suspend_status()
            if output_sink is not None
            else contextlib.nullcontext()
        )
        with status_suspension:
            was_watching = self.input_manager.is_watching_for_interrupt
            if was_watching:
                self.input_manager.stop_watching_for_interrupt()

            self.console.print()
            self.console.print(
                f"[bold cyan]{escape_rich_markup(spec.label)} required[/bold cyan]\n"
                "[dim]Input is masked and will not be added to chat history or logs.[/dim]"
            )
            try:
                value = await self.input_manager.get_secret(
                    f"<b>{escape_html(spec.label)}:</b> ",
                )
            except (KeyboardInterrupt, EOFError):
                self.console.print("[yellow]Credential entry cancelled.[/yellow]")
                return None
            finally:
                if was_watching:
                    self.input_manager.reset_interrupt()
                    self.input_manager.start_watching_for_interrupt()

            value = value.strip()
            if not value:
                self.console.print(
                    "[yellow]Credential was empty; nothing changed.[/yellow]"
                )
                return None
            return CredentialInput(value=value, persist=persist)

    async def prompt_for_ollama_base_url(self) -> Optional[str]:
        """Collect an Ollama API endpoint outside normal chat history."""

        output_sink = getattr(self, "output_sink", None)
        status_suspension = (
            output_sink.suspend_status()
            if output_sink is not None
            else contextlib.nullcontext()
        )
        with status_suspension:
            was_watching = self.input_manager.is_watching_for_interrupt
            if was_watching:
                self.input_manager.stop_watching_for_interrupt()

            configured = get_config(reload=True).ollama_base
            default_url = configured or DEFAULT_OLLAMA_BASE_URL
            self.console.print()
            self.console.print(
                "[bold cyan]Ollama server URL required[/bold cyan]\n"
                "[dim]Enter the Ollama API endpoint. For a local server, keep "
                f"{DEFAULT_OLLAMA_BASE_URL}.[/dim]"
            )
            try:
                while True:
                    try:
                        value = await self.input_manager.get_text(
                            "<b>Ollama server URL:</b> ",
                            default=default_url,
                        )
                    except (KeyboardInterrupt, EOFError):
                        self.console.print(
                            "[yellow]Ollama server entry cancelled.[/yellow]"
                        )
                        return None

                    try:
                        return _normalize_ollama_base_url(value or default_url)
                    except ValueError as exc:
                        self.console.print(f"[yellow]{exc}[/yellow]")
                        default_url = value.strip() or default_url
            finally:
                if was_watching:
                    self.input_manager.reset_interrupt()
                    self.input_manager.start_watching_for_interrupt()

    def _codex_auth_is_configured(self) -> bool:
        """Return whether fresh runtime configuration contains Codex auth."""

        config = get_config(reload=True)
        return bool(config.codex_access_token or config.codex_oauth_refresh_token)

    async def prompt_for_codex_login(self) -> bool:
        """Run browser OAuth as part of interactive Codex provider setup."""

        output_sink = getattr(self, "output_sink", None)
        status_suspension = (
            output_sink.suspend_status()
            if output_sink is not None
            else contextlib.nullcontext()
        )
        with status_suspension:
            was_watching = self.input_manager.is_watching_for_interrupt
            if was_watching:
                self.input_manager.stop_watching_for_interrupt()

            self.console.print()
            self.console.print(
                "[bold cyan]Sign in to Codex with your browser[/bold cyan]"
            )
            self.console.print(
                "[dim]Complete sign-in in the browser, then return here. "
                "Kerkov will activate Codex automatically.[/dim]"
            )
            self.console.print()

            try:
                config = get_config(reload=True)
                success, message = await asyncio.to_thread(
                    run_codex_oauth_browser_login,
                    config,
                )
            except (KeyboardInterrupt, EOFError):
                self.console.print("[yellow]Codex sign-in cancelled.[/yellow]")
                self.console.print()
                return False
            except Exception as exc:
                logger.error("Codex browser sign-in failed: %s", exc)
                self.console.print(f"[bold red]Codex sign-in failed:[/bold red] {exc}")
                self.console.print()
                return False
            finally:
                if was_watching:
                    self.input_manager.reset_interrupt()
                    self.input_manager.start_watching_for_interrupt()

        if not success:
            self.console.print(f"[bold red]Codex sign-in failed:[/bold red] {message}")
            self.console.print()
            return False

        self.console.print(f"[green]✓ {message}[/green]")
        self.console.print()
        return True

    async def activate_provider_interactive(
        self,
        provider_name: str,
        *,
        validate_existing: bool = False,
    ) -> bool:
        """Prompt for a missing key, validate it, and activate the provider."""

        normalized_provider = provider_name.strip().lower()
        is_codex = normalized_provider in {"codex", "openai-codex"}
        codex_login_performed = False
        if is_codex and not self._codex_auth_is_configured():
            if not await self.prompt_for_codex_login():
                return False
            codex_login_performed = True

        ollama_base: Optional[str] = None
        if normalized_provider == "ollama":
            ollama_base = await self.prompt_for_ollama_base_url()
            if ollama_base is None:
                return False

        spec = provider_credential_spec(provider_name)
        prompted: Optional[CredentialInput] = None
        if spec is not None and not self.credentials.get(spec):
            prompted = await self.prompt_for_credential(spec, True)
            if prompted is None:
                return False
            self.credentials.set_session(spec, prompted.value)

        try:
            if ollama_base is not None:
                candidate = self.provder_manager.build_provider(
                    provider_name,
                    config_overrides={"ollama_base": ollama_base},
                )
            else:
                candidate = self.provder_manager.build_provider(provider_name)
        except Exception as exc:
            if prompted is not None and spec is not None:
                self.credentials.forget_session(spec)
            self.console.print(f"[bold red]Error:[/bold red] {exc}")
            self.console.print()
            return False

        if (
            prompted is not None
            or validate_existing
            or ollama_base is not None
            or is_codex
        ):
            try:
                valid = await asyncio.to_thread(candidate.validate_connection)
            except Exception:
                valid = False

            # Token presence is not proof of usable authentication. A stale
            # access token or revoked refresh token can build a candidate but
            # fail catalog validation. Recover through the same browser-login
            # path once, then rebuild so it reads the freshly persisted state.
            if not valid and is_codex and not codex_login_performed:
                self.console.print(
                    "[yellow]Stored Codex sign-in could not be validated. "
                    "Starting browser sign-in again.[/yellow]"
                )
                if not await self.prompt_for_codex_login():
                    return False
                codex_login_performed = True

                try:
                    candidate = self.provder_manager.build_provider(provider_name)
                except Exception as exc:
                    self.console.print(f"[bold red]Error:[/bold red] {exc}")
                    self.console.print()
                    return False

                try:
                    valid = await asyncio.to_thread(candidate.validate_connection)
                except Exception:
                    valid = False

            if not valid:
                if prompted is not None and spec is not None:
                    self.credentials.forget_session(spec)
                provider_label = PROVIDER_DISPLAY_NAMES.get(
                    provider_name.strip().lower(),
                    provider_name,
                )
                self.console.print(
                    f"[bold red]Error:[/bold red] {provider_label} "
                    "could not be validated."
                )
                self.console.print()
                return False

        if prompted is not None and spec is not None:
            save_result = self.credentials.save(
                spec,
                prompted.value,
                persist=prompted.persist,
            )
            if save_result.warning:
                self.console.print(f"[yellow]{save_result.warning}[/yellow]")
            elif save_result.persisted:
                self.console.print(
                    "[dim]Credential saved in the "
                    f"{credential_storage_description(save_result.storage)}.[/dim]"
                )

        if ollama_base is not None:
            try:
                settings_path = save_user_setting("OLLAMA_BASE", ollama_base)
            except Exception:
                self.console.print(
                    "[yellow]The Ollama server URL could not be saved; it will "
                    "remain active for this session only.[/yellow]"
                )
            else:
                self.console.print(
                    f"[dim]Ollama server URL saved in {settings_path}.[/dim]"
                )

        self.provder_manager.set_provider(candidate)
        await self._reconfigure_context_for_provider()
        model_selection_notice = getattr(candidate, "model_selection_notice", None)
        if isinstance(model_selection_notice, str) and model_selection_notice:
            self.console.print(f"[yellow]{model_selection_notice}[/yellow]")
        selected_provider = self.current_provider_name()
        provider_label = PROVIDER_DISPLAY_NAMES.get(
            selected_provider, selected_provider
        )
        try:
            save_user_setting("LLM_PROVIDER", selected_provider)
        except Exception:
            self.console.print(
                "[yellow]The provider preference could not be saved; this "
                "selection is active for this session only.[/yellow]"
            )
        override = os.getenv("LLM_PROVIDER", "").strip().lower()
        if override and override != selected_provider:
            self.console.print(
                "[yellow]LLM_PROVIDER in the environment or .env overrides this "
                "saved selection on restart. Remove that override to use the "
                "remembered provider.[/yellow]"
            )
        self.console.print(f"[green]✓[/green] Switched provider to {provider_label}")
        self.console.print()
        return True

    async def _initialize_provider(self) -> None:
        """Initialize the LLM provider based on configuration."""

        try:
            self.provder_manager.initialize_provider()
            provider = self.provder_manager.provider

            if not provider:
                configured = self.config.llm_provider
                self.console.print(
                    "[bold yellow]Warning:[/bold yellow] No LLM provider "
                    f"initialized (configured: {configured})."
                )
                self.console.print(
                    "[dim]Use /provider to choose a provider. Selecting Codex "
                    "starts browser sign-in when needed.[/dim]"
                )
                return

            if not await asyncio.to_thread(provider.validate_connection):
                self.console.print(
                    "[bold yellow]Provider unavailable:[/bold yellow] "
                    "The configured LLM provider could not be reached."
                )
                self.provder_manager.clear_provider()
                return

            model_selection_notice = getattr(
                provider,
                "model_selection_notice",
                None,
            )
            if isinstance(model_selection_notice, str) and model_selection_notice:
                self.console.print(f"[yellow]{model_selection_notice}[/yellow]")

            self.console.print(
                f"[green]✓[/green] Connected to {provider.model}",
                style="dim",
            )

        except (MissingCredentialError, MissingCodexAuthError) as exc:
            logger.info(
                "Configured provider is waiting for interactive credential setup "
                "(provider=%s)",
                exc.provider_name,
            )
            return
        except Exception as exc:
            configured = self.config.llm_provider
            self.console.print(
                f"[bold red]Error:[/bold red] {str(exc)}", style="bold red"
            )
            if configured.strip().lower() == "codex":
                self.console.print(
                    "[dim]Starting without a provider. Select Codex from the "
                    "provider picker to sign in.[/dim]"
                )
                return
            logger.error(
                "Exception occurred in KerkovCLI._initialize_provider: %s", exc
            )
            sys.exit(1)

    def current_provider_name(self) -> Optional[str]:
        """Return the canonical name of the active provider, if configured."""

        provider = self.provder_manager.provider
        if provider is None:
            return None
        provider_name = str(provider.get_model_info()["provider"])
        return provider_name.replace("Provider", "").strip().lower()

    async def select_provider_interactive(
        self,
        *,
        allow_setup_later: bool = False,
    ) -> Optional[str]:
        """Choose a provider, highlighting the active one when present."""

        provider = self.provder_manager.provider
        current_provider = self.current_provider_name()
        current_model = None
        if current_provider is not None and provider is not None:
            current_model = str(provider.get_model_info()["model"])
        options: list[tuple[str, AnyFormattedText]] = []
        for provider_name in list_providers():
            label: AnyFormattedText
            if provider_name == current_provider:
                label = active_choice_label(
                    f"{PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)} "
                    f"— {current_model}"
                )
            else:
                spec = provider_credential_spec(provider_name)
                if spec is not None:
                    status = (
                        "credential configured"
                        if self.credentials.get(spec)
                        else "API key required"
                    )
                elif provider_name == "ollama":
                    status = "server URL required; no API key"
                elif provider_name == "codex":
                    status = "browser sign-in"
                else:
                    status = "no API key"
                label = (
                    f"{PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)} "
                    f"— {status}"
                )
            options.append((provider_name, label))

        if allow_setup_later:
            options.append(
                (
                    SETUP_LATER_PROVIDER,
                    "Continue without a provider — setup mode",
                )
            )
        selected = await self.input_manager.choose(
            "Select an LLM provider",
            options,
            default=(current_provider or self.config.llm_provider.strip().lower()),
        )
        if selected in {None, SETUP_LATER_PROVIDER}:
            return None
        return selected

    async def _select_startup_provider(self) -> Optional[str]:
        """Offer provider onboarding when startup has no usable default."""

        return await self.select_provider_interactive(allow_setup_later=True)

    def _display_provider_setup_guidance(self) -> None:
        """Explain the usable CLI state after default-provider setup is skipped."""

        self.console.print()
        self.console.print(
            "[bold yellow]No LLM provider is active; Kerkov is in setup mode.[/bold yellow]"
        )
        self.console.print(
            "[dim]Use `/provider` to open the provider picker, or "
            "`/provider <name>` to select one directly. Commands such as "
            "`/help`, `/auth`, and `/q` remain available.[/dim]"
        )
        self.console.print()

    @abstractmethod
    async def _reconfigure_context_for_provider(self) -> None:
        """Reconfigure provider-specific context after activation."""

        raise NotImplementedError
