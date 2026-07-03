"""
Platform-aware shell wrapping for the process subsystem.

Both the PTY and child adapters need to run a single command string through a
shell. The wrapping differs by platform — and, on Windows, by transport:

  * POSIX wraps with ``bash -c`` and hands the argv straight to
    ``execvp``/``PtyProcess.spawn`` (no re-quoting), so ``shell_wrap()`` returns
    an argv list.
  * Windows must hand the shell a *verbatim* command line, because both
    ``subprocess.list2cmdline`` and pywinpty backslash-escape embedded quotes,
    which ``cmd`` then chokes on. So Windows callers use ``windows_pty_spec()``
    (for ConPTY's low-level ``PTY.spawn``) and ``create_subprocess_shell`` (for
    the child adapter), both of which preserve quoting the way a user typing at
    a prompt would expect.

The shell is selectable via ``WINDOWS_DEFAULT_SHELL`` (cmd | powershell | pwsh)
or an explicit ``shell=`` override. ``cmd`` is the default for compatibility.
PowerShell/pwsh PTY invocations use ``-EncodedCommand`` so quoted arguments are
not re-parsed by the ConPTY command line layer.
"""

from __future__ import annotations

import base64
import os
import sys
from typing import Optional

WINDOWS_SHELLS: dict[str, list[str]] = {
    "cmd": ["cmd", "/c"],
    "powershell": ["powershell", "-NoLogo", "-NoProfile", "-Command"],
    "pwsh": ["pwsh", "-NoLogo", "-NoProfile", "-Command"],
}

DEFAULT_WINDOWS_SHELL = "cmd"


def default_shell() -> str:
    """Return the configured default shell name for the current platform."""
    if sys.platform == "win32":
        configured = os.environ.get("WINDOWS_DEFAULT_SHELL", "").strip().lower()
        if configured in WINDOWS_SHELLS:
            return configured
        return DEFAULT_WINDOWS_SHELL
    return "bash"


def resolve_shell(shell: Optional[str] = None) -> str:
    """Resolve an explicit override (or the platform default) to a shell name."""
    name = (shell or default_shell()).strip().lower()
    if sys.platform == "win32" and name not in WINDOWS_SHELLS:
        return DEFAULT_WINDOWS_SHELL
    return name


def shell_wrap(command: str, shell: Optional[str] = None) -> list[str]:
    """
    Wrap a command string in the right shell invocation for this platform.

    Returns an argv list suitable for ``PtyProcess.spawn`` (POSIX) or
    ``asyncio.create_subprocess_exec``. On Windows this argv form is subject to
    ``list2cmdline`` re-quoting — see ``windows_pty_spec`` and the child
    adapter's ``create_subprocess_shell`` path for the verbatim alternatives.
    """
    name = resolve_shell(shell)

    if sys.platform == "win32":
        return [*WINDOWS_SHELLS[name], command]

    return [name or "bash", "-c", command]


def windows_pty_spec(command: str, shell: Optional[str] = None) -> tuple[str, str]:
    """
    Build a (appname, cmdline) pair for pywinpty's low-level ``PTY.spawn``.

    ``appname`` is the shell executable; ``cmdline`` is the *verbatim* argument
    tail (leading space, no list2cmdline escaping). For PowerShell shells, the
    command is encoded as UTF-16LE for ``-EncodedCommand`` so embedded quotes and
    spaces survive the Windows command line boundary.
    """
    name = resolve_shell(shell)

    if name == "cmd":
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        return comspec, f' /d /s /c "{command}"'

    exe = f"{name}.exe"
    encoded = _powershell_encoded_command(command)
    return exe, f" -NoLogo -NoProfile -EncodedCommand {encoded}"


def _powershell_encoded_command(command: str) -> str:
    """Encode a PowerShell command for -EncodedCommand."""
    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")
