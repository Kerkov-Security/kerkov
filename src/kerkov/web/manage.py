"""Management entrypoint for the Kerkov Django web app."""

from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

from django.core.management import execute_from_command_line

DEFAULT_RUNSERVER_ADDRPORT = "127.0.0.1:8764"


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run a Django management command for the bundled Kerkov web app."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")
    args = list(argv) if argv is not None else list(sys.argv)
    if len(args) == 2 and args[1] == "runserver":
        args.append(DEFAULT_RUNSERVER_ADDRPORT)
    execute_from_command_line(args)


if __name__ == "__main__":
    main()
