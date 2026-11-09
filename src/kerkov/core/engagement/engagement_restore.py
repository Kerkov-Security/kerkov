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
    aliases: dict[str, str] | None = None,
) -> Any:
    """Look a persisted setting up in its enum, or say what is wrong with it.

    `aliases` maps a legacy stored value (uppercased when `by_name`) to the
    current member it was renamed to, so older engagements still restore.
    """
    lookup = saved.upper() if by_name else saved
    if aliases and lookup in aliases:
        lookup = aliases[lookup]
    try:
        return enum_cls[lookup] if by_name else enum_cls(lookup)
    except (KeyError, ValueError):
        known = ", ".join(m.value for m in enum_cls)
        raise EngagementRestoreError(
            f"Engagement '{engagement_id}' records {label} '{saved}', which this "
            f"build does not have. Known values: {known}."
        ) from None
