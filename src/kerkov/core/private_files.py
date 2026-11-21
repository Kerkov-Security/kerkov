"""Permission helpers for Kerkov-managed private files."""

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping


def prepare_private_parent(parent: Path, *, managed_parent: Path) -> None:
    """Create a private parent without changing unrelated existing directories.

    A newly created leaf directory is always tightened to ``0700`` on POSIX.
    An existing directory is only tightened when it is the canonical Kerkov
    data directory and is owned by the effective user. This prevents an
    environment override from chmodding shared locations such as ``/tmp`` or a
    repository root.
    """

    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        parent_created = True
    except FileExistsError:
        parent_created = False

    if os.name == "nt":
        if not parent.is_dir() or parent.is_symlink():
            raise PermissionError("Private parent must be a real directory.")
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        directory_fd = os.open(parent, flags)
    except OSError as exc:
        if parent.is_symlink():
            message = "Private parent must not be a symlink."
        elif parent_created and isinstance(exc, PermissionError):
            message = (
                "New private parent permissions prevent secure validation; "
                "owner read and execute access are required."
            )
        else:
            message = "Private parent could not be securely opened as a directory."
        raise PermissionError(message) from exc

    try:
        parent_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise PermissionError("Private parent must be a directory.")

        if parent_created:
            if parent_stat.st_uid != os.geteuid():
                raise PermissionError(
                    "New private parent is not owned by the effective user."
                )
            os.fchmod(directory_fd, 0o700)
            hardened_mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
            if hardened_mode != 0o700:
                raise PermissionError(
                    "New private parent could not be hardened to mode 0700."
                )
            return

        try:
            managed_stat = os.stat(managed_parent, follow_symlinks=False)
        except OSError:
            return

        is_managed_parent = (
            stat.S_ISDIR(managed_stat.st_mode)
            and parent_stat.st_dev == managed_stat.st_dev
            and parent_stat.st_ino == managed_stat.st_ino
        )
        if is_managed_parent and parent_stat.st_uid == os.geteuid():
            os.fchmod(directory_fd, 0o700)
    finally:
        os.close(directory_fd)


def write_private_json(
    path: Path, data: Mapping[str, Any], *, managed_parent: Path, prefix: str
) -> None:
    """Atomically replace a JSON file with owner-only permissions."""
    prepare_private_parent(path.parent, managed_parent=managed_parent)
    fd, name = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
    temporary = Path(name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(dict(data), stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
