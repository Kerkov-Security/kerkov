"""Automatic lifecycle and onboarding for Kerkov's OWASP ZAP runtime."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from rich.markup import escape as escape_rich_markup

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.config_manager import ConfigManager, get_config, save_user_setting
from kerkov.core.credentials import (
    ZAP_API_KEY,
    credential_storage_description,
)
from kerkov.core.proxy import (
    DockerZapRuntime,
    ProxyAdapterError,
    ProxyConnectionInfo,
    ZapAdapter,
    ZapRuntimeError,
    choose_loopback_service_url,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI


class ZapSetupMessage(UserMessage):
    """Start, verify, report, or stop Kerkov's managed ZAP daemon."""

    def __init__(self, action: str = "status") -> None:
        self.action = action

    async def execute(self, cli: "KerkovCLI") -> None:
        if self.action == "setup":
            await self._setup(cli)
            return
        if self.action == "status":
            await self._status(cli)
            return
        if self.action == "stop":
            await self._stop(cli)
            return
        cli.console.print(
            "[bold red]Error:[/bold red] Use `/zap setup`, `/zap status`, "
            "or `/zap stop`."
        )
        cli.console.print()

    async def _setup(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        existing_key = cli.credentials.get(ZAP_API_KEY)
        if existing_key:
            try:
                existing_adapter = _adapter(config, existing_key)
                info = await existing_adapter.probe()
            except (ProxyAdapterError, ValueError):
                pass
            else:
                _print_ready(cli, info, already_running=True)
                return

        if not config.execution_enabled:
            _print_failure(
                cli,
                "Process execution is disabled. Set EXECUTION_ENABLED=true "
                "before starting the managed ZAP container.",
            )
            return

        try:
            runtime = _runtime(config)
            await runtime.preflight()
            service_url = choose_loopback_service_url(config.zap_api_url)
        except (ValueError, ZapRuntimeError) as exc:
            _print_failure(cli, str(exc))
            return

        api_key = existing_key or secrets.token_urlsafe(32)
        cli.console.print()
        cli.console.print("[bold cyan]Starting OWASP ZAP[/bold cyan]")
        cli.console.print(
            "[dim]Kerkov will use the official stable ZAP Docker image, bind "
            "the API only to loopback, and protect it with a generated key. "
            "The first setup may take a few minutes while Docker downloads "
            "the image.[/dim]"
        )

        try:
            runtime_info = await runtime.start(
                service_url=service_url,
                api_key=api_key,
            )
            cli.console.print(
                "[dim]ZAP container started. Waiting for its API to become "
                f"ready (up to {config.zap_startup_timeout_seconds} seconds)...[/dim]"
            )
            adapter = ZapAdapter(
                service_url=runtime_info.service_url,
                api_key=api_key,
                timeout_seconds=float(config.zap_api_timeout_seconds),
                verify_tls=config.zap_api_verify_tls,
                allow_remote=False,
            )
            info = await wait_for_zap(
                adapter,
                timeout_seconds=float(config.zap_startup_timeout_seconds),
            )
        except (ProxyAdapterError, ValueError, ZapRuntimeError) as exc:
            diagnostic = await _safe_diagnostic(runtime)
            await _safe_stop(runtime)
            _print_failure(
                cli,
                str(exc),
                diagnostic=_redact(diagnostic, api_key),
            )
            return

        try:
            settings_path = save_user_setting("ZAP_API_URL", runtime_info.service_url)
        except Exception:
            await _safe_stop(runtime)
            _print_failure(
                cli,
                "The validated ZAP URL could not be saved. The managed "
                "container was stopped and the API key was not stored.",
            )
            return

        save_result = None
        if existing_key is None:
            save_result = cli.credentials.save(ZAP_API_KEY, api_key, persist=True)

        if save_result is not None and save_result.warning:
            cli.console.print(f"[yellow]{save_result.warning}[/yellow]")
        elif save_result is not None and save_result.persisted:
            cli.console.print(
                "[dim]Generated API key saved in the "
                f"{credential_storage_description(save_result.storage)}.[/dim]"
            )

        _print_ready(cli, info, already_running=False)
        cli.console.print(
            f"[dim]Service URL saved in "
            f"{escape_rich_markup(str(settings_path))}. The zap tool is ready "
            "now; no Kerkov restart is required.[/dim]"
        )
        cli.console.print()

    async def _status(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        api_key = cli.credentials.get(ZAP_API_KEY)
        if not api_key:
            cli.console.print(
                "[yellow]OWASP ZAP is not configured. Run `/zap setup` to "
                "generate a key and start it.[/yellow]"
            )
            cli.console.print()
            return
        try:
            info = await _adapter(config, api_key).probe()
        except (ProxyAdapterError, ValueError) as exc:
            _print_failure(cli, str(exc))
            return
        _print_ready(cli, info, already_running=True)
        cli.console.print()

    async def _stop(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        if not config.execution_enabled:
            _print_failure(
                cli,
                "Process execution is disabled. Set EXECUTION_ENABLED=true "
                "before stopping the managed ZAP container.",
            )
            return
        try:
            stopped = await _runtime(config).stop()
        except (ValueError, ZapRuntimeError) as exc:
            _print_failure(cli, str(exc))
            return
        if stopped:
            cli.console.print("[green]✓[/green] Stopped Kerkov's ZAP container.")
        else:
            cli.console.print("[yellow]Kerkov's ZAP container is not running.[/yellow]")
        cli.console.print()


async def wait_for_zap(
    adapter: ZapAdapter,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ProxyConnectionInfo:
    """Poll locally until ZAP is ready, preserving the last safe failure."""

    deadline = now_fn() + timeout_seconds
    last_error = "ZAP did not become ready"
    while now_fn() < deadline:
        try:
            return await adapter.probe()
        except ProxyAdapterError as exc:
            last_error = str(exc)
        await sleep_fn(poll_interval_seconds)
    raise ProxyAdapterError(
        f"ZAP did not become ready within {timeout_seconds:g} seconds: {last_error}"
    )


def _adapter(config: ConfigManager, api_key: str) -> ZapAdapter:
    return ZapAdapter(
        service_url=config.zap_api_url,
        api_key=api_key,
        timeout_seconds=float(config.zap_api_timeout_seconds),
        verify_tls=config.zap_api_verify_tls,
        allow_remote=config.zap_api_allow_remote,
    )


def _runtime(config: ConfigManager) -> DockerZapRuntime:
    return DockerZapRuntime(
        image=config.zap_docker_image,
        container_name=config.zap_docker_container_name,
    )


async def _safe_stop(runtime: DockerZapRuntime) -> None:
    try:
        await runtime.stop()
    except ZapRuntimeError:
        pass


async def _safe_diagnostic(runtime: DockerZapRuntime) -> str:
    try:
        return await runtime.diagnostic_tail()
    except ZapRuntimeError:
        return ""


def _print_ready(
    cli: "KerkovCLI",
    info: ProxyConnectionInfo,
    *,
    already_running: bool,
) -> None:
    state = "already running" if already_running else "ready"
    version = f" {escape_rich_markup(info.version)}" if info.version else ""
    cli.console.print(
        f"[green]✓[/green] OWASP ZAP{version} is {state} at "
        f"{escape_rich_markup(info.service_url)}."
    )


def _print_failure(cli: "KerkovCLI", message: str, *, diagnostic: str = "") -> None:
    cli.console.print(
        f"[bold red]ZAP setup failed:[/bold red] " f"{escape_rich_markup(message)}"
    )
    if diagnostic:
        cli.console.print(
            "[dim]Container log tail:\n"
            f"{escape_rich_markup(diagnostic[-2000:])}[/dim]"
        )
    cli.console.print()


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value
