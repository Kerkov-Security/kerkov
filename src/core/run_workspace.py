"""Run-scoped workspace directories for engagement artifacts."""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path
from typing import Optional


DEFAULT_WORKSPACE_ROOT = Path.home() / ".kerkov" / "workspaces"

_current_workspace: ContextVar[Optional[Path]] = ContextVar(
    "kerkov_current_workspace",
    default=None,
)


class RunWorkspaceManager:
    """Creates and tracks workspace directories keyed by engagement ID."""

    def __init__(self, root: str | Path = DEFAULT_WORKSPACE_ROOT):
        self.root = Path(root).expanduser()
        self._active_path: Optional[Path] = None

    @property
    def active_path(self) -> Optional[Path]:
        return self._active_path

    def path_for_engagement(self, engagement_id: str) -> Path:
        return (self.root / engagement_id).expanduser().resolve()

    def activate(self, engagement_id: str) -> Path:
        path = self.path_for_engagement(engagement_id)
        ensure_workspace_dirs(path)
        self._active_path = path
        return path


def ensure_workspace_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def set_current_workspace(path: Optional[Path]) -> Token:
    """Set the current async context's workspace and return a reset token."""
    return _current_workspace.set(path.expanduser().resolve() if path else None)


def reset_current_workspace(token) -> None:
    _current_workspace.reset(token)


def get_current_workspace(manager: Optional[RunWorkspaceManager] = None) -> Optional[Path]:
    """Return the async-context workspace, falling back to a manager default."""
    path = _current_workspace.get()
    if path is not None:
        return path
    if manager is not None:
        return manager.active_path
    return None
