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
    ApprovalMode.YOLO: "No prompts; approval rules skipped",
}

APPROVAL_MODE_COLORS: dict[ApprovalMode, str] = {
    ApprovalMode.STRICT: "blue",
    ApprovalMode.NORMAL: "green",
    ApprovalMode.PERMISSIVE: "yellow",
    ApprovalMode.YOLO: "red",
}


class ApprovalModeManager:
    """Manages which tool calls require user approval."""

    def __init__(self, initial: ApprovalMode = ApprovalMode.NORMAL):
        self._current = initial
        logger.info(f"ApprovalModeManager initialized: {self._current.value}")

    @property
    def current(self) -> ApprovalMode:
        return self._current

    def set(self, mode: ApprovalMode) -> ApprovalMode:
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
