"""
Autonomy preset management for Kerkov Bot.

`AutonomyLevel` 1-4 is now a preset bundle that sets both `Verbosity` and
`ApprovalMode` at once. The two axes are independent and can be set
individually via `VerbosityManager` and `ApprovalModeManager`; the preset
exists for backwards compatibility with `/autonomy <n>`, `AUTONOMY_LEVEL`,
and legacy engagement metadata.
"""

from enum import IntEnum
from typing import Optional

from src.cli.approval_mode_manager import ApprovalMode, ApprovalModeManager
from src.cli.verbosity_manager import Verbosity, VerbosityManager
from src.core.logging.logger_factory import get_logger

logger = get_logger("AutonomyManager")


class AutonomyLevel(IntEnum):
    """Tiered preset levels from most supervised to fully autonomous."""

    GUIDED = 1
    STANDARD = 2
    AGGRESSIVE = 3
    AUTONOMOUS = 4


AUTONOMY_DESCRIPTIONS: dict[AutonomyLevel, str] = {
    AutonomyLevel.GUIDED: "Detailed explanations, all tools require approval",
    AutonomyLevel.STANDARD: "Brief explanations, unmatched calls prompt; deny rules enforced",
    AutonomyLevel.AGGRESSIVE: "Minimal explanations, unmatched calls auto-approve; deny rules enforced",
    AutonomyLevel.AUTONOMOUS: "No explanations, no approval prompts (requires opt-in)",
}

AUTONOMY_COLORS: dict[AutonomyLevel, str] = {
    AutonomyLevel.GUIDED: "blue",
    AutonomyLevel.STANDARD: "green",
    AutonomyLevel.AGGRESSIVE: "yellow",
    AutonomyLevel.AUTONOMOUS: "red",
}


AUTONOMY_PRESETS: dict[AutonomyLevel, tuple[Verbosity, ApprovalMode]] = {
    AutonomyLevel.GUIDED: (Verbosity.GUIDED, ApprovalMode.STRICT),
    AutonomyLevel.STANDARD: (Verbosity.STANDARD, ApprovalMode.NORMAL),
    AutonomyLevel.AGGRESSIVE: (Verbosity.AGGRESSIVE, ApprovalMode.PERMISSIVE),
    AutonomyLevel.AUTONOMOUS: (Verbosity.SILENT, ApprovalMode.YOLO),
}


def preset_for(level: AutonomyLevel) -> tuple[Verbosity, ApprovalMode]:
    """Return the (verbosity, approval_mode) pair for an autonomy preset."""
    return AUTONOMY_PRESETS[level]


def derive_autonomy_level(
    verbosity: Verbosity, approval_mode: ApprovalMode
) -> Optional[AutonomyLevel]:
    """Reverse lookup: if (verbosity, approval_mode) matches a preset, return it."""
    for level, pair in AUTONOMY_PRESETS.items():
        if pair == (verbosity, approval_mode):
            return level
    return None


class AutonomyManager:
    """Thin facade that sets both Verbosity and ApprovalMode at preset levels.

    Wraps a `VerbosityManager` + `ApprovalModeManager` pair so legacy callers
    (CLI flag `--autonomy`, slash `/autonomy`, engagement metadata, env var
    `AUTONOMY_LEVEL`) keep working unchanged.
    """

    def __init__(
        self,
        initial_level: int = 2,
        allow_autonomous: bool = False,
        verbosity_manager: Optional[VerbosityManager] = None,
        approval_mode_manager: Optional[ApprovalModeManager] = None,
    ):
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

        level = AutonomyLevel(initial_level)
        verbosity, approval_mode = preset_for(level)

        self._verbosity_manager = verbosity_manager or VerbosityManager(initial=verbosity)
        self._approval_mode_manager = approval_mode_manager or ApprovalModeManager(
            initial=approval_mode, allow_autonomous=allow_autonomous
        )

        if verbosity_manager is None:
            self._verbosity_manager.set(verbosity)
        if approval_mode_manager is None:
            self._approval_mode_manager.set(approval_mode)

        logger.info(
            f"AutonomyManager initialized as preset facade: level={level.name}, "
            f"verbosity={verbosity.value}, approval_mode={approval_mode.value}, "
            f"allow_autonomous={allow_autonomous}"
        )

    @property
    def verbosity_manager(self) -> VerbosityManager:
        return self._verbosity_manager

    @property
    def approval_mode_manager(self) -> ApprovalModeManager:
        return self._approval_mode_manager

    @property
    def current_level(self) -> AutonomyLevel:
        """Derive a preset level from the current (verbosity, approval) pair.

        Falls back to STANDARD when the axes have drifted off-preset (e.g. the
        user set them independently via /verbosity or /approval).
        """
        derived = derive_autonomy_level(
            self._verbosity_manager.current, self._approval_mode_manager.current
        )
        return derived or AutonomyLevel.STANDARD

    @property
    def allow_autonomous(self) -> bool:
        return self._allow_autonomous

    def set_level(self, level: int) -> AutonomyLevel:
        """Apply the preset for the given level to both axes."""
        if level < 1 or level > 4:
            raise ValueError(
                f"Autonomy level must be between 1 and 4, got {level}"
            )

        if level == AutonomyLevel.AUTONOMOUS and not self._allow_autonomous:
            raise ValueError(
                "Autonomy level 4 (Autonomous) requires ALLOW_AUTONOMOUS=true "
                "in .env or as an environment variable"
            )

        target = AutonomyLevel(level)
        verbosity, approval_mode = preset_for(target)
        self._verbosity_manager.set(verbosity)
        self._approval_mode_manager.set(approval_mode)
        logger.info(
            f"Autonomy preset applied: {target.name} "
            f"(verbosity={verbosity.value}, approval_mode={approval_mode.value})"
        )
        return target

    def list_levels(self) -> dict[AutonomyLevel, str]:
        return AUTONOMY_DESCRIPTIONS.copy()

    @staticmethod
    def parse_level(value: str) -> Optional[AutonomyLevel]:
        """Parse a level from "1"-"4" or a level name."""
        value = value.strip().lower()

        try:
            num = int(value)
            if 1 <= num <= 4:
                return AutonomyLevel(num)
            return None
        except ValueError:
            pass

        name_map = {level.name.lower(): level for level in AutonomyLevel}
        return name_map.get(value)
