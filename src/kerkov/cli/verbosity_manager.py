"""
Verbosity management for Kerkov.

Controls only one thing: how much the LLM narrates its reasoning. This is
the explanation-verbosity half of the old `AutonomyLevel`; approval gating
lives in `approval_mode_manager.py`.
"""

from enum import Enum
from typing import Optional

from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("VerbosityManager")


class Verbosity(str, Enum):
    """How much the LLM narrates its reasoning."""

    GUIDED = "guided"
    STANDARD = "standard"
    AGGRESSIVE = "aggressive"
    SILENT = "silent"


VERBOSITY_DESCRIPTIONS: dict[Verbosity, str] = {
    Verbosity.GUIDED: "Per-tool intent before each call; post-call interpretation",
    Verbosity.STANDARD: "Batch-level intent before tool sequences; summary after",
    Verbosity.AGGRESSIVE: "Phase-level explanations only; no per-call narration",
    Verbosity.SILENT: "No narration; tools execute without explanation",
}

VERBOSITY_COLORS: dict[Verbosity, str] = {
    Verbosity.GUIDED: "blue",
    Verbosity.STANDARD: "green",
    Verbosity.AGGRESSIVE: "yellow",
    Verbosity.SILENT: "red",
}


class VerbosityManager:
    """Manages how much the LLM narrates its reasoning."""

    def __init__(self, initial: Verbosity = Verbosity.STANDARD):
        self._current = initial
        logger.info(f"VerbosityManager initialized: {self._current.value}")

    @property
    def current(self) -> Verbosity:
        return self._current

    def set(self, verbosity: Verbosity) -> Verbosity:
        previous = self._current
        self._current = verbosity
        logger.info(f"Verbosity switched: {previous.value} -> {self._current.value}")
        return self._current

    def list_levels(self) -> dict[Verbosity, str]:
        return VERBOSITY_DESCRIPTIONS.copy()

    @staticmethod
    def parse(value: str) -> Optional[Verbosity]:
        """Parse a verbosity from a string name."""
        normalized = value.strip().lower()
        try:
            return Verbosity(normalized)
        except ValueError:
            return None
