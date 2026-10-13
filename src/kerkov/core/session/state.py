"""The managers and stores a Kerkov session owns, built in one place.

`SessionState` is the shared Model: the managers both the CLI and the web
console used to construct identically in their own `__init__`s. Front ends build
one with `build_session_state` and keep their own UI-specific objects (console,
WebSocket bus, approval strategy, tools, agent supervisor) beside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from kerkov.agents.agent_manager import AgentManager
from kerkov.agents.agent_registry import AgentRegistry
from kerkov.context.skills.skill_manager import SkillManager
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import CredentialManager
from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.core.run_workspace import RunWorkspaceManager
from kerkov.core.session.approval_mode_manager import ApprovalMode, ApprovalModeManager
from kerkov.core.session.autonomy_manager import (
    AutonomyLevel,
    AutonomyManager,
    preset_for,
)
from kerkov.core.session.mode_manager import ModeManager
from kerkov.core.session.modes import ModeType
from kerkov.core.session.verbosity_manager import Verbosity, VerbosityManager
from kerkov.providers.provider_manager import ProviderManager


@dataclass
class SessionState:
    """The managers and stores shared by every front end for one session."""

    config: ConfigManager
    credentials: CredentialManager
    provider_manager: ProviderManager
    mode_manager: ModeManager
    verbosity_manager: VerbosityManager
    approval_mode_manager: ApprovalModeManager
    autonomy_manager: AutonomyManager
    workspace_manager: RunWorkspaceManager
    skill_manager: SkillManager
    agent_manager: AgentManager
    agent_registry: AgentRegistry
    engagement_store: EngagementStore
    token_tracker: TokenTracker


def build_session_state(
    config: ConfigManager,
    credentials: CredentialManager,
    *,
    mode: Optional[ModeType] = None,
) -> SessionState:
    """Build the managers a session needs, deriving the autonomy axes identically.

    Verbosity and approval start from the autonomy preset, overridden by their
    explicit config values when set — the same derivation both front ends used.
    """
    preset_verbosity, preset_approval = preset_for(AutonomyLevel(config.autonomy_level))
    verbosity_manager = VerbosityManager(
        initial=Verbosity(config.verbosity) if config.verbosity else preset_verbosity
    )
    approval_mode_manager = ApprovalModeManager(
        initial=(
            ApprovalMode(config.approval_mode)
            if config.approval_mode
            else preset_approval
        ),
        allow_autonomous=config.allow_autonomous,
    )
    autonomy_manager = AutonomyManager(
        initial_level=config.autonomy_level,
        allow_autonomous=config.allow_autonomous,
        verbosity_manager=verbosity_manager,
        approval_mode_manager=approval_mode_manager,
    )
    return SessionState(
        config=config,
        credentials=credentials,
        provider_manager=ProviderManager(config, credentials),
        mode_manager=ModeManager(initial_mode=mode),
        verbosity_manager=verbosity_manager,
        approval_mode_manager=approval_mode_manager,
        autonomy_manager=autonomy_manager,
        workspace_manager=RunWorkspaceManager(config.workspace_dir),
        skill_manager=SkillManager(),
        agent_manager=AgentManager(),
        agent_registry=AgentRegistry(
            max_depth=config.agent_max_depth,
            max_total=config.agent_max_total,
        ),
        engagement_store=EngagementStore(),
        token_tracker=TokenTracker(),
    )
