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
    """Creates workspace directories keyed by engagement ID.

    Activation sets the workspace into the async-context `ContextVar` that
    tools read from. The manager itself only computes paths and creates
    directories — it does not hold mutable "active" state, so the same
    object can be shared safely across concurrent agent runs.
    """

    def __init__(self, root: str | Path = DEFAULT_WORKSPACE_ROOT):
        self.root = Path(root).expanduser()

    def path_for_engagement(self, engagement_id: str) -> Path:
        root = self.root.resolve()
        path = (self.root / engagement_id).expanduser().resolve()
        # Guard against engagement IDs that escape the workspace root via
        # path traversal (e.g. "..", "/absolute", "foo/../../bar").
        if path != root and root not in path.parents:
            raise ValueError(
                f"Engagement ID {engagement_id!r} resolves outside workspace "
                f"root {root}"
            )
        return path

    def activate(self, engagement_id: str) -> Path:
        path = self.path_for_engagement(engagement_id)
        ensure_workspace_dirs(path)
        _current_workspace.set(path)
        return path


def ensure_workspace_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def set_current_workspace(path: Optional[Path]) -> Token:
    """Set the current async context's workspace and return a reset token."""
    return _current_workspace.set(path.expanduser().resolve() if path else None)


def reset_current_workspace(token: Token) -> None:
    _current_workspace.reset(token)


def get_current_workspace() -> Optional[Path]:
    """Return the workspace path active in the current async context."""
    return _current_workspace.get()
