"""Resolve runtime assets in a checkout, user directory, or installed wheel."""

from importlib import resources
from pathlib import Path
from typing import Optional


def _find_project_root(start_dir: Path) -> Optional[Path]:
    current = start_dir.resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "kerkov" / "core" / "project_paths.py"
        ).is_file():
            return candidate

    return None


def resolve_project_dir(name: str, start_dir: Optional[Path] = None) -> Path:
    """Prefer checkout assets, then user assets, then bundled defaults.

    Never discover assets in an unrelated engagement's working directory.
    ``name`` may also be a relative file path, allowing per-file config fallback.
    """
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Asset paths must be relative to the Kerkov data directory")
    root = _find_project_root(start_dir or Path(__file__))
    if root is not None and (root / relative).exists():
        return root / relative

    user_path = Path.home() / ".kerkov" / relative
    if user_path.exists():
        return user_path
    return bundled_asset_path(name)


def bundled_asset_path(name: str) -> Path:
    """Return a bundled asset, using its source location for editable installs."""
    root = _find_project_root(Path(__file__))
    if root is not None:
        return root / name
    # pip and uv unpack wheels on disk; keeping real paths also lets skill
    # companion scripts execute relative to their SKILL.md.
    return Path(str(resources.files("kerkov").joinpath("bundled", name)))
