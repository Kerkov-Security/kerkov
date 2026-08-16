"""Status and stop commands for Kerkov's automatically managed ZAP runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as escape_rich_markup

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.config_manager import (
    ConfigManager,
    get_config,
    remove_user_settings,
)
from kerkov.core.credentials import ZAP_API_KEY
from kerkov.core.proxy import (
    DockerZapRuntime,
    ProxyAdapterError,
    ProxyConnectionInfo,
    ZapAdapter,
    ZapRuntimeError,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI


class ZapMessage(UserMessage):
    """Report or stop Kerkov's automatically managed ZAP daemon."""

    def __init__(self, action: str = "status") -> None:
        self.action = action

    async def execute(self, cli: "KerkovCLI") -> None:
        if self.action == "status":
            await self._status(cli)
            return
        if self.action == "stop":
            await self._stop(cli)
            return
        cli.console.print(
            "[bold red]Error:[/bold red] Use `/zap status` or `/zap stop`."
        )
        cli.console.print()

    async def _status(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        api_key = cli.credentials.get(ZAP_API_KEY)
        if not api_key:
            cli.console.print(
                "[yellow]OWASP ZAP is not running yet. It starts automatically "
                "with the first spider or active scan.[/yellow]"
            )
            cli.console.print()
            return
        try:
            info = await _adapter(config, api_key).probe()
        except (ProxyAdapterError, ValueError) as exc:
            _print_failure(cli, str(exc))
            return
        _print_ready(cli, info)
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
        if config.zap_managed:
            try:
                remove_user_settings("ZAP_API_URL", "ZAP_MANAGED")
            except (OSError, ValueError) as exc:
                _print_failure(
                    cli,
                    "The container was stopped, but Kerkov could not reset its "
                    f"managed ZAP settings: {exc}",
                )
                return
            warning = cli.credentials.delete(ZAP_API_KEY)
            cli.console.print(
                "[green]✓[/green] Reset Kerkov's managed ZAP state. The next "
                "scan will start ZAP automatically."
            )
            if warning:
                cli.console.print(f"[yellow]{warning}[/yellow]")
        cli.console.print()


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


def _print_ready(cli: "KerkovCLI", info: ProxyConnectionInfo) -> None:
    version = f" {escape_rich_markup(info.version)}" if info.version else ""
    cli.console.print(
        f"[green]✓[/green] OWASP ZAP{version} is running at "
        f"{escape_rich_markup(info.service_url)}."
    )


def _print_failure(cli: "KerkovCLI", message: str) -> None:
    cli.console.print(
        f"[bold red]ZAP command failed:[/bold red] " f"{escape_rich_markup(message)}"
    )
    cli.console.print()
