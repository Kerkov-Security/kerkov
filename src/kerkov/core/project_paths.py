"""Helpers for resolving project-local paths from package modules."""

from pathlib import Path
from typing import Optional


def _find_project_root(start_dir: Path) -> Optional[Path]:
    current = start_dir.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    return None


def resolve_project_dir(name: str, start_dir: Optional[Path] = None) -> Path:
    """Resolve a top-level project directory from source or the current project."""
    if start_dir is not None:
        root = _find_project_root(start_dir)
        if root is not None:
            return root / name

    root = _find_project_root(Path.cwd())
    return (root or Path.cwd()) / name
