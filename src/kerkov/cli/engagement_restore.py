"""Validating the settings a saved engagement records before restoring them.

Separate from `kerkov_cli` so the message handlers it imports can catch this.
"""

from typing import Any


class EngagementRestoreError(Exception):
    """A saved engagement names a setting this build cannot restore."""


def restored_setting(
    enum_cls: Any,
    saved: str,
    engagement_id: str,
    label: str,
    *,
    by_name: bool = False,
) -> Any:
    """Look a persisted setting up in its enum, or say what is wrong with it."""
    try:
        return enum_cls[saved.upper()] if by_name else enum_cls(saved)
    except (KeyError, ValueError):
        known = ", ".join(m.value for m in enum_cls)
        raise EngagementRestoreError(
            f"Engagement '{engagement_id}' records {label} '{saved}', which this "
            f"build does not have. Known values: {known}."
        ) from None
