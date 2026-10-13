"""Startup interface selection: terminal CLI or the browser web console."""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import time
import webbrowser
from typing import Optional

from rich.console import Console
from rich.prompt import Confirm, Prompt

from kerkov.core.config_manager import save_user_setting

INTERFACE_SETTING = "INTERFACE"
VALID_INTERFACES = ("cli", "web")
DEFAULT_WEB_ADDRPORT = "127.0.0.1:8764"
WEB_READY_TIMEOUT_SECONDS = 20.0


def resolve_interface(
    explicit: Optional[str],
    saved: Optional[str],
    wants_cli_session: bool,
) -> Optional[str]:
    """Resolve the interface, or None when an interactive prompt is warranted.

    An explicit ``--interface`` wins; a CLI-only invocation (--message /
    --continue / --resume) forces ``cli`` over a saved ``web`` default; otherwise
    the saved preference applies, and None means nothing decided it yet.
    """

    if explicit in VALID_INTERFACES:
        return explicit
    if wants_cli_session:
        return "cli"
    if saved in VALID_INTERFACES:
        return saved
    return None


def prompt_for_interface(console: Console) -> str:
    """Ask the operator which interface to start and offer to remember it."""

    console.print()
    console.print("[bold]How would you like to run Kerkov?[/bold]")
    console.print("  [cyan]cli[/cyan]  Interactive terminal assistant (default)")
    console.print(
        "  [cyan]web[/cyan]  Browser console on "
        f"http://{DEFAULT_WEB_ADDRPORT} (local-only, unauthenticated)"
    )
    choice = Prompt.ask(
        "Interface",
        choices=list(VALID_INTERFACES),
        default="cli",
        console=console,
    )

    if Confirm.ask(
        f"Remember [cyan]{choice}[/cyan] as your default? "
        "(change it any time with --interface)",
        default=True,
        console=console,
    ):
        try:
            save_user_setting(INTERFACE_SETTING, choice)
            console.print("[dim]Saved. Run with --interface cli|web to override.[/dim]")
        except (OSError, ValueError) as exc:
            console.print(f"[yellow]Could not save preference:[/yellow] {exc}")

    return choice


def _web_dependencies_available() -> bool:
    return (
        importlib.util.find_spec("django") is not None
        and importlib.util.find_spec("daphne") is not None
    )


def _split_addrport(addrport: str) -> tuple[str, int]:
    host, _, port = addrport.rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port)


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _terminate(process: "subprocess.Popen[bytes]") -> int:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return process.returncode or 0


def launch_web_console(
    console: Console,
    addrport: str = DEFAULT_WEB_ADDRPORT,
) -> int:
    """Start the Django/Channels web console and open it in a browser."""

    if not _web_dependencies_available():
        console.print(
            "[bold red]The web console requires the 'web' extra.[/bold red]\n"
            "Install it with [cyan]uv sync --extra web[/cyan] "
            '(or [cyan]pip install "kerkov[web]"[/cyan]), '
            "then run [cyan]uv run kerkov[/cyan] again.\n"
            "To stay in the terminal, start with [cyan]--interface cli[/cyan]."
        )
        return 1

    host, port = _split_addrport(addrport)
    url = f"http://{addrport}"
    console.print("[bold]Starting the Kerkov web console...[/bold]")

    process = subprocess.Popen(
        [sys.executable, "-m", "kerkov.web.manage", "runserver", addrport]
    )
    try:
        if _wait_for_port(host, port, WEB_READY_TIMEOUT_SECONDS):
            console.print(f"  → {url}  (opening browser)")
            try:
                webbrowser.open(url)
            except Exception:
                console.print(
                    f"[dim]Could not open a browser automatically; visit {url}.[/dim]"
                )
        else:
            console.print(f"  → {url}")
            console.print(
                "[yellow]Server did not report ready in time; "
                "it may still be starting.[/yellow]"
            )
        console.print("[dim]Press Ctrl-C to stop the server.[/dim]")
        return process.wait()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopping the web console...[/dim]")
        _terminate(process)
        return 0
    finally:
        _terminate(process)
