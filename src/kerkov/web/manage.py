"""Management entrypoint for the Kerkov Django web app."""

from __future__ import annotations

import ipaddress
import os
import sys
from typing import Optional, Sequence

from dotenv import find_dotenv, load_dotenv

try:
    from daphne.management.commands.runserver import Command as RunserverCommand
    from django import setup as django_setup
    from django.conf import settings
    from django.core.management import execute_from_command_line
except ModuleNotFoundError:
    RunserverCommand = None
    django_setup = None
    settings = None
    execute_from_command_line = None

DEFAULT_RUNSERVER_ADDRPORT = "127.0.0.1:8764"


def _bind_host(addrport: str) -> str:
    if addrport.isdigit():
        return "127.0.0.1"
    if addrport.startswith("[") and "]" in addrport:
        return addrport[1 : addrport.index("]")]
    if ":" in addrport:
        return addrport.rsplit(":", 1)[0]
    return addrport


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _runserver_addrport(args: Sequence[str]) -> Optional[str]:
    if "runserver" not in args:
        return None
    if RunserverCommand is None:
        return None
    runserver_index = args.index("runserver")
    parser = RunserverCommand().create_parser(args[0], "runserver")
    options = parser.parse_args(list(args[runserver_index + 1 :]))
    addrport = options.addrport
    return str(addrport) if addrport is not None else None


def _missing_web_dependencies() -> SystemExit:
    return SystemExit(
        'Django web dependencies are not installed. Install "kerkov[web]" '
        'or run "uv sync --extra web".'
    )


def _load_web_environment() -> None:
    """Load the same working-tree .env file used by the Kerkov runtime."""

    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path)


def _guard_local_runserver(args: Sequence[str]) -> None:
    if "runserver" not in args:
        return
    addrport = _runserver_addrport(args) or DEFAULT_RUNSERVER_ADDRPORT
    host = _bind_host(addrport)
    if not _is_loopback_host(host):
        raise SystemExit(
            "kerkov-web is unauthenticated and may only bind to localhost. "
            "Remote/LAN hosting is not supported."
        )


def _enable_static_serving(args: list[str]) -> None:
    """Serve bundled assets when the local runserver uses DEBUG=False."""

    if (
        "runserver" in args
        and settings is not None
        and not settings.DEBUG
        and "--insecure" not in args
        and "--nostatic" not in args
    ):
        args.append("--insecure")


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run a Django management command for the bundled Kerkov web app."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")
    args = list(argv) if argv is not None else list(sys.argv)
    if (
        execute_from_command_line is None
        or RunserverCommand is None
        or django_setup is None
    ):
        raise _missing_web_dependencies()
    _load_web_environment()
    django_setup()
    if len(args) >= 2 and args[1] == "runserver" and _runserver_addrport(args) is None:
        args.append(DEFAULT_RUNSERVER_ADDRPORT)
    _guard_local_runserver(args)
    _enable_static_serving(args)
    execute_from_command_line(args)


if __name__ == "__main__":
    main()
