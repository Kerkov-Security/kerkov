"""Shared session domain layer: the managers/stores every front end owns.

The CLI and the web console assembled the same set of managers in their
constructors; `build_session_state` builds them once so the two cannot drift.
"""

from kerkov.core.session.kernel import SessionKernel
from kerkov.core.session.state import SessionState, build_session_state

__all__ = ["SessionState", "SessionKernel", "build_session_state"]
