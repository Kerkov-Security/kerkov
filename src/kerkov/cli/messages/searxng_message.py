"""Setup and lifecycle commands for Kerkov's managed SearXNG search backend."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from rich.markup import escape as escape_rich_markup

from kerkov.cli.messages.user_message import UserMessage
from kerkov.core.config_manager import (
    ConfigManager,
    get_config,
    remove_user_settings,
    save_user_setting,
)
from kerkov.core.websearch.searxng_runtime import (
    DEFAULT_BASE_URL,
    DockerSearxngRuntime,
    SearxngRuntimeError,
    SearxngRuntimeInfo,
    choose_loopback_base_url,
    normalize_searxng_base_url,
    probe_json_api,
    probe_searxng,
    wait_for_searxng,
)

if TYPE_CHECKING:
    from kerkov.cli.kerkov_cli import KerkovCLI


class SearxngMessage(UserMessage):
    """Provision, start, stop or inspect Kerkov's managed SearXNG instance."""

    def __init__(self, action: str = "status") -> None:
        self.action = action

    async def execute(self, cli: "KerkovCLI") -> None:
        if self.action == "status":
            await self._status(cli)
            return
        if self.action == "setup":
            await self._setup(cli)
            return
        if self.action == "start":
            await self._start(cli)
            return
        if self.action == "stop":
            await self._stop(cli)
            return
        cli.console.print(
            "[bold red]Error:[/bold red] Use `/searxng setup`, `/searxng start`, "
            "`/searxng stop` or `/searxng status`."
        )
        cli.console.print()

    async def _status(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        runtime = _runtime(config)
        if not runtime.is_provisioned():
            cli.console.print(
                "[yellow]SearXNG is not set up yet. Run `/searxng setup` to "
                "install and start a local instance.[/yellow]"
            )
            cli.console.print()
            return
        try:
            state = await runtime.state()
        except SearxngRuntimeError as exc:
            _print_failure(cli, str(exc))
            return
        if not state.running:
            cli.console.print(
                "[yellow]Kerkov's SearXNG container is not running. Start it "
                "with `/searxng start`.[/yellow]"
            )
            cli.console.print()
            return

        base_url = _configured_base_url(config)
        if base_url is None:
            _print_failure(
                cli,
                "The container is running, but SEARCH_BASE_URL does not point at "
                "a loopback instance. Run `/searxng setup` to reconfigure it.",
            )
            return
        try:
            version = await probe_searxng(
                base_url,
                timeout_seconds=float(config.search_timeout_seconds),
            )
        except SearxngRuntimeError as exc:
            _print_failure(cli, str(exc))
            return
        _print_ready(cli, base_url, version)
        cli.console.print()

    async def _setup(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        if not _require_execution(cli, config, "provisioning SearXNG"):
            return
        runtime = _runtime(config)
        try:
            await runtime.preflight()
            # Free the previous port before probing for one, or re-running setup
            # would walk the instance forward one port at a time.
            await runtime.stop()
            base_url = choose_loopback_base_url(
                _configured_base_url(config) or DEFAULT_BASE_URL
            )
            settings_path = runtime.provision()
            cli.console.print(
                f"[green]✓[/green] Wrote {escape_rich_markup(str(settings_path))} "
                "with `json` enabled in `search.formats`."
            )
            cli.console.print(
                "[dim]Pulling the SearXNG image (first run can take a while)…[/dim]"
            )
            info = await runtime.start(base_url=base_url)
        except (OSError, SearxngRuntimeError, ValueError) as exc:
            await _print_start_failure(cli, runtime, str(exc))
            return
        await self._verify_and_save(cli, config, runtime, info)

    async def _start(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        if not _require_execution(cli, config, "starting SearXNG"):
            return
        runtime = _runtime(config)
        if not runtime.is_provisioned():
            _print_failure(
                cli,
                "SearXNG is not set up yet. Run `/searxng setup` first.",
            )
            return
        base_url = _configured_base_url(config)
        if base_url is None:
            _print_failure(
                cli,
                "SEARCH_BASE_URL does not point at a loopback instance. Run "
                "`/searxng setup` to reconfigure it.",
            )
            return
        try:
            state = await runtime.state()
            if state.running:
                # A start whose JSON probe failed leaves the container up with
                # SEARCH_PROVIDER unsaved, so an already-running instance still
                # goes through verification: otherwise the retry that is meant
                # to finish that start would only report the container as up.
                cli.console.print(
                    "[yellow]Kerkov's SearXNG container is already running.[/yellow]"
                )
                info = SearxngRuntimeInfo(
                    base_url=base_url,
                    container_name=runtime.container_name,
                    image=runtime.image,
                    image_pulled=False,
                )
            else:
                info = await runtime.start(base_url=base_url)
        except (OSError, SearxngRuntimeError, ValueError) as exc:
            await _print_start_failure(cli, runtime, str(exc))
            return
        await self._verify_and_save(cli, config, runtime, info)

    async def _stop(self, cli: "KerkovCLI") -> None:
        config = get_config(reload=True)
        if not _require_execution(cli, config, "stopping SearXNG"):
            return
        try:
            stopped = await _runtime(config).stop()
        except (SearxngRuntimeError, ValueError) as exc:
            _print_failure(cli, str(exc))
            return
        if stopped:
            cli.console.print("[green]✓[/green] Stopped Kerkov's SearXNG container.")
        else:
            cli.console.print(
                "[yellow]Kerkov's SearXNG container is not running.[/yellow]"
            )
        # SEARCH_BASE_URL is deliberately kept: it is the port `/searxng start`
        # brings the same instance back on. Only the provider selection is
        # reset, so web_search does not stay pointed at a dead instance.
        try:
            remove_user_settings("SEARCH_PROVIDER")
        except (OSError, ValueError) as exc:
            _print_failure(
                cli,
                "The container was stopped, but Kerkov could not reset the "
                f"managed search provider: {exc}",
            )
            return
        cli.reload_tools()
        cli.console.print(
            "[dim]Run `/searxng start` to bring it back on the same port.[/dim]"
        )
        cli.console.print()

    async def _verify_and_save(
        self,
        cli: "KerkovCLI",
        config: ConfigManager,
        runtime: DockerSearxngRuntime,
        info: SearxngRuntimeInfo,
    ) -> None:
        if info.image_pulled:
            cli.console.print(
                f"[green]✓[/green] Pulled {escape_rich_markup(info.image)}."
            )
        try:
            version = await wait_for_searxng(
                info.base_url,
                timeout_seconds=float(config.searxng_startup_timeout_seconds),
            )
            results = await probe_json_api(
                info.base_url,
                timeout_seconds=float(config.search_timeout_seconds),
            )
        except SearxngRuntimeError as exc:
            await _print_start_failure(cli, runtime, str(exc))
            return
        try:
            save_user_setting("SEARCH_BASE_URL", info.base_url)
            save_user_setting("SEARCH_PROVIDER", "searxng")
        except (OSError, ValueError) as exc:
            _print_failure(
                cli,
                "SearXNG is running, but Kerkov could not persist its search "
                f"settings: {exc}",
            )
            return
        cli.reload_tools()
        _print_ready(cli, info.base_url, version)
        cli.console.print(
            f"[green]✓[/green] The JSON results API answered with {results} "
            "results for a probe query."
        )
        cli.console.print("[dim]Search tools reloaded for this session.[/dim]")
        cli.console.print()


def _runtime(config: ConfigManager) -> DockerSearxngRuntime:
    return DockerSearxngRuntime(
        image=config.searxng_docker_image,
        container_name=config.searxng_docker_container_name,
        config_dir=Path(config.searxng_config_dir),
    )


def _configured_base_url(config: ConfigManager) -> Optional[str]:
    """The configured instance URL, or None when it is not a managed loopback one."""

    try:
        return normalize_searxng_base_url(config.search_base_url or "")
    except ValueError:
        return None


def _require_execution(cli: "KerkovCLI", config: ConfigManager, action: str) -> bool:
    if config.execution_enabled:
        return True
    _print_failure(
        cli,
        f"Process execution is disabled. Set EXECUTION_ENABLED=true before {action}.",
    )
    return False


async def _print_start_failure(
    cli: "KerkovCLI",
    runtime: DockerSearxngRuntime,
    message: str,
) -> None:
    detail = message
    try:
        diagnostic = await runtime.diagnostic_tail()
    except SearxngRuntimeError:
        diagnostic = ""
    if diagnostic:
        detail = f"{detail} Container log tail: {diagnostic}"
    _print_failure(cli, detail)


def _print_ready(cli: "KerkovCLI", base_url: str, version: str) -> None:
    labeled = f" {escape_rich_markup(version)}" if version else ""
    cli.console.print(
        f"[green]✓[/green] SearXNG{labeled} is running at "
        f"{escape_rich_markup(base_url)}."
    )


def _print_failure(cli: "KerkovCLI", message: str) -> None:
    cli.console.print(
        f"[bold red]SearXNG command failed:[/bold red] {escape_rich_markup(message)}"
    )
    cli.console.print()
