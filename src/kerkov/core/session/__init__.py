"""Shared session domain layer: the managers/stores every front end owns.

The CLI and the web console assembled the same set of managers in their
constructors; `build_session_state` builds them once so the two cannot drift.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kerkov.core.session.kernel import SessionKernel
    from kerkov.core.session.state import SessionState, build_session_state

__all__ = ["SessionState", "SessionKernel", "build_session_state"]


def __getattr__(name: str) -> Any:
    # Resolve the package API on first access, not at package import. `kernel`
    # (and `state`, through it) import `conversation_engine`, which imports the
    # session-config leaf modules living in this package (approval_mode_manager,
    # modes, ...). Importing one of those leaves must not drag the kernel in, or
    # the engine and the kernel form an import cycle.
    if name == "SessionKernel":
        from kerkov.core.session.kernel import SessionKernel

        return SessionKernel
    if name in ("SessionState", "build_session_state"):
        from kerkov.core.session import state

        return getattr(state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
