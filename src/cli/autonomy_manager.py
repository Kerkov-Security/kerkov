"""
Autonomy level management for Kerkov Bot.

Controls two linked behaviors:
1. Explanation verbosity — how much the LLM narrates its reasoning
2. Approval gating — whether/when the user must approve tool execution
"""

from enum import IntEnum
from typing import Optional

from src.core.logging.logger_factory import get_logger

logger = get_logger("AutonomyManager")


class AutonomyLevel(IntEnum):
    """Tiered autonomy levels from most supervised to fully autonomous."""

    GUIDED = 1
    STANDARD = 2
    AGGRESSIVE = 3
    AUTONOMOUS = 4


AUTONOMY_DESCRIPTIONS: dict[AutonomyLevel, str] = {
    AutonomyLevel.GUIDED: "Detailed explanations, all tools require approval",
    AutonomyLevel.STANDARD: "Brief explanations, per-tool approval levels respected",
    AutonomyLevel.AGGRESSIVE: "Minimal explanations, only REQUIRED tools prompt",
    AutonomyLevel.AUTONOMOUS: "No explanations, no approval prompts (requires opt-in)",
}

AUTONOMY_COLORS: dict[AutonomyLevel, str] = {
    AutonomyLevel.GUIDED: "blue",
    AutonomyLevel.STANDARD: "green",
    AutonomyLevel.AGGRESSIVE: "yellow",
    AutonomyLevel.AUTONOMOUS: "red",
}


class AutonomyManager:
    """Manages the current autonomy level for Kerkov Bot."""

    def __init__(
        self,
        initial_level: int = 2,
        allow_autonomous: bool = False,
    ):
        """
        Initialize the autonomy manager.

        Args:
            initial_level: Starting autonomy level (1-4, default 2)
            allow_autonomous: Whether level 4 (Autonomous) is permitted
        """
        self._allow_autonomous = allow_autonomous

        if initial_level < 1 or initial_level > 4:
            raise ValueError(
                f"Autonomy level must be between 1 and 4, got {initial_level}"
            )

        if initial_level == AutonomyLevel.AUTONOMOUS and not allow_autonomous:
            raise ValueError(
                "Autonomy level 4 (Autonomous) requires ALLOW_AUTONOMOUS=true "
                "in .env or as an environment variable"
            )

        self._current_level = AutonomyLevel(initial_level)
        logger.info(
            f"AutonomyManager initialized: level={self._current_level.name}, "
            f"allow_autonomous={allow_autonomous}"
        )

    @property
    def current_level(self) -> AutonomyLevel:
        """Get the current autonomy level."""
        return self._current_level

    @property
    def allow_autonomous(self) -> bool:
        """Whether level 4 (Autonomous) is permitted."""
        return self._allow_autonomous

    def set_level(self, level: int) -> AutonomyLevel:
        """
        Switch to a different autonomy level.

        Args:
            level: Target level (1-4)

        Returns:
            The new current level

        Raises:
            ValueError: If level is out of range or level 4 is not permitted
        """
        if level < 1 or level > 4:
            raise ValueError(
                f"Autonomy level must be between 1 and 4, got {level}"
            )

        if level == AutonomyLevel.AUTONOMOUS and not self._allow_autonomous:
            raise ValueError(
                "Autonomy level 4 (Autonomous) requires ALLOW_AUTONOMOUS=true "
                "in .env or as an environment variable"
            )

        previous = self._current_level
        self._current_level = AutonomyLevel(level)
        logger.info(
            f"Autonomy level switched: {previous.name} -> {self._current_level.name}"
        )
        return self._current_level

    def list_levels(self) -> dict[AutonomyLevel, str]:
        """
        Get all available levels with descriptions.

        Returns:
            Dictionary mapping autonomy levels to descriptions
        """
        return AUTONOMY_DESCRIPTIONS.copy()

    @staticmethod
    def parse_level(value: str) -> Optional[AutonomyLevel]:
        """
        Parse a level from string input.

        Accepts "1"-"4" or level names ("guided", "standard", "aggressive",
        "autonomous").

        Args:
            value: String to parse

        Returns:
            AutonomyLevel if valid, None otherwise
        """
        value = value.strip().lower()

        # Try numeric
        try:
            num = int(value)
            if 1 <= num <= 4:
                return AutonomyLevel(num)
            return None
        except ValueError:
            pass

        # Try name
        name_map = {level.name.lower(): level for level in AutonomyLevel}
        return name_map.get(value)
