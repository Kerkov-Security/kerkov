"""
Approval-mode management for Kerkov.

Controls only one thing: which tool calls prompt the user before executing.
This is the approval-gating half of the old `AutonomyLevel`; explanation
verbosity lives in `verbosity_manager.py`.
"""

from enum import Enum
from typing import Optional

from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("ApprovalModeManager")


class ApprovalMode(str, Enum):
    """Approval gating for tool calls."""

    STRICT = "strict"
    NORMAL = "normal"
    PERMISSIVE = "permissive"
    YOLO = "yolo"


APPROVAL_MODE_DESCRIPTIONS: dict[ApprovalMode, str] = {
    ApprovalMode.STRICT: "Deny rules apply; allow rules ignored; all others prompt",
    ApprovalMode.NORMAL: "Rules-based: allow/deny rules evaluated; unmatched calls prompt",
    ApprovalMode.PERMISSIVE: "Rules-based: deny rules apply; unmatched calls auto-approved",
    ApprovalMode.YOLO: "No prompts (requires ALLOW_AUTONOMOUS=true)",
}

APPROVAL_MODE_COLORS: dict[ApprovalMode, str] = {
    ApprovalMode.STRICT: "blue",
    ApprovalMode.NORMAL: "green",
    ApprovalMode.PERMISSIVE: "yellow",
    ApprovalMode.YOLO: "red",
}


class ApprovalModeManager:
    """Manages which tool calls require user approval."""

    def __init__(
        self,
        initial: ApprovalMode = ApprovalMode.NORMAL,
        allow_autonomous: bool = False,
    ):
        self._allow_autonomous = allow_autonomous

        if initial == ApprovalMode.YOLO and not allow_autonomous:
            raise ValueError(
                "Approval mode 'yolo' requires ALLOW_AUTONOMOUS=true "
                "in .env or as an environment variable"
            )

        self._current = initial
        logger.info(
            f"ApprovalModeManager initialized: {self._current.value}, "
            f"allow_autonomous={allow_autonomous}"
        )

    @property
    def current(self) -> ApprovalMode:
        return self._current

    @property
    def allow_autonomous(self) -> bool:
        return self._allow_autonomous

    def set(self, mode: ApprovalMode) -> ApprovalMode:
        if mode == ApprovalMode.YOLO and not self._allow_autonomous:
            raise ValueError(
                "Approval mode 'yolo' requires ALLOW_AUTONOMOUS=true "
                "in .env or as an environment variable"
            )

        previous = self._current
        self._current = mode
        logger.info(
            f"Approval mode switched: {previous.value} -> {self._current.value}"
        )
        return self._current

    def list_modes(self) -> dict[ApprovalMode, str]:
        return APPROVAL_MODE_DESCRIPTIONS.copy()

    @staticmethod
    def parse(value: str) -> Optional[ApprovalMode]:
        normalized = value.strip().lower()
        try:
            return ApprovalMode(normalized)
        except ValueError:
            return None
