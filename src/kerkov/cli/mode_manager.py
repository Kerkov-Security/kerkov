"""
Mode management for Kerkov.

Handles switching between operational modes and managing mode state.
"""

from typing import Optional

from kerkov.cli.modes import (
    AUDIT_MODE,
    CTF_MODE,
    OFFENSIVE_MODE,
    RECON_MODE,
    Mode,
    ModeType,
)
from kerkov.core.logging.logger_factory import get_logger

logger = get_logger("ModeManager")


class ModeManager:
    """Manages operational mode for Kerkov."""

    def __init__(self, initial_mode: Optional[ModeType] = None):
        """
        Initialize the mode manager.

        Args:
            initial_mode: Starting mode (defaults to AUDIT)
        """
        self._modes = {
            ModeType.RECON: RECON_MODE,
            ModeType.AUDIT: AUDIT_MODE,
            ModeType.OFFENSIVE: OFFENSIVE_MODE,
            ModeType.CTF: CTF_MODE,
        }
        self._current_mode_type = initial_mode or ModeType.AUDIT
        logger.info(
            f"ModeManager initialized with mode: {self._current_mode_type.value}"
        )

    @property
    def current_mode(self) -> Mode:
        """Get the current operational mode."""
        return self._modes[self._current_mode_type]

    @property
    def current_mode_type(self) -> ModeType:
        """Get the current mode type."""
        return self._current_mode_type

    def switch_mode(self, mode_type: ModeType) -> Mode:
        """
        Switch to a different operational mode.

        Args:
            mode_type: The mode to switch to

        Returns:
            The new current mode
        """
        previous = self._current_mode_type
        self._current_mode_type = mode_type
        logger.info(f"Mode switched: {previous.value} -> {mode_type.value}")
        return self.current_mode

    def get_mode(self, mode_type: ModeType) -> Mode:
        """
        Get a specific mode without switching to it.

        Args:
            mode_type: The mode type to retrieve

        Returns:
            The requested mode
        """
        return self._modes[mode_type]

    def list_modes(self) -> dict[ModeType, Mode]:
        """
        Get all available modes.

        Returns:
            Dictionary mapping mode types to mode objects
        """
        return self._modes.copy()

    @staticmethod
    def parse_mode_string(mode_str: str) -> Optional[ModeType]:
        """
        Parse a mode string into a ModeType.

        Args:
            mode_str: String representation of mode (e.g., "recon", "audit", "offensive")

        Returns:
            ModeType if valid, None otherwise
        """
        mode_str = mode_str.lower().strip()
        try:
            return ModeType(mode_str)
        except ValueError:
            return None
