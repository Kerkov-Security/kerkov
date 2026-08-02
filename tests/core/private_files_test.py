"""Tests for private file-parent permission handling."""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.private_files import prepare_private_parent


def test_new_private_parent_is_created_without_path_chmod(tmp_path: Path) -> None:
    parent = tmp_path / "custom" / "private"

    with patch("kerkov.core.private_files.os.chmod") as path_chmod:
        prepare_private_parent(parent, managed_parent=tmp_path / ".kerkov")

    assert parent.is_dir()
    path_chmod.assert_not_called()
    if os.name != "nt":
        assert stat.S_IMODE(parent.stat().st_mode) & 0o077 == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow directory open")
def test_new_parent_symlink_race_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent = tmp_path / "private"
    target = tmp_path / "attacker-controlled"
    target.mkdir()
    target_mode = stat.S_IMODE(target.stat().st_mode)
    original_mkdir = Path.mkdir

    def raced_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == parent:
            path.symlink_to(target, target_is_directory=True)
            raise FileExistsError(str(path))
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", raced_mkdir)

    with pytest.raises(PermissionError, match="must not be a symlink"):
        prepare_private_parent(parent, managed_parent=tmp_path / ".kerkov")

    assert stat.S_IMODE(target.stat().st_mode) == target_mode


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_new_parent_restores_owner_write_permission_after_umask(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    previous_umask = os.umask(0o200)
    try:
        prepare_private_parent(parent, managed_parent=tmp_path / ".kerkov")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_new_parent_with_unopenable_mode_reports_permissions(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    previous_umask = os.umask(0o700)
    try:
        with pytest.raises(PermissionError, match="permissions prevent"):
            prepare_private_parent(parent, managed_parent=tmp_path / ".kerkov")
    finally:
        os.umask(previous_umask)
        if parent.exists():
            os.chmod(parent, 0o700)
