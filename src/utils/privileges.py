"""
Privilege / elevation checks for the current process.

Used to gate operations that genuinely need root (POSIX) or Administrator
(Windows) — e.g. opening tun/tap devices for VPN, raw sockets, binding
privileged ports, or any tool that prompts for sudo.
"""

import os

def is_process_elevated() -> bool:
    """Return True if the current process is running as root (POSIX) or
    Administrator (Windows). Returns False on any unexpected error so
    callers fail closed rather than assuming privilege."""
    try:
        if os.name == "posix":
            return os.geteuid() == 0
        if os.name == "nt":
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
    return False
