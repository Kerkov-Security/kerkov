"""Private persistent browser profiles with exclusive process ownership."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kerkov.core.private_files import prepare_private_parent


def prepare_profile(root: Path, name: str) -> Path:
    prepare_private_parent(root, managed_parent=root)
    profile = root / name
    prepare_private_parent(profile, managed_parent=profile)
    return profile


class ProfileLease:
    """Hold a nonblocking OS lock until a browser profile has closed."""

    def __init__(self, profile: Path) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self.fd: int | None = os.open(profile / ".kerkov.lock", flags, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
