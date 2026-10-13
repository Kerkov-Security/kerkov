"""build_session_state assembles the managers both front ends share."""

import unittest

from kerkov.core.config_manager import ConfigManager
from kerkov.core.credentials import get_credential_manager
from kerkov.core.session import SessionState, build_session_state
from kerkov.core.session.autonomy_manager import preset_for
from kerkov.core.session.mode_manager import ModeManager
from kerkov.core.session.modes import ModeType


def _state(**overrides) -> SessionState:
    config = ConfigManager(GOOGLE_API_KEY="test", **overrides)
    return build_session_state(config, get_credential_manager())


class BuildSessionStateTest(unittest.TestCase):

    def test_builds_every_shared_manager(self):
        state = _state()
        for name in (
            "provider_manager",
            "mode_manager",
            "verbosity_manager",
            "approval_mode_manager",
            "autonomy_manager",
            "workspace_manager",
            "skill_manager",
            "agent_manager",
            "agent_registry",
            "engagement_store",
            "token_tracker",
        ):
            self.assertIsNotNone(getattr(state, name), name)

    def test_mode_defaults_to_audit_and_honors_override(self):
        self.assertEqual(
            _state().mode_manager.current_mode_type, ModeManager().current_mode_type
        )
        state = build_session_state(
            ConfigManager(GOOGLE_API_KEY="test"),
            get_credential_manager(),
            mode=ModeType.RECON,
        )
        self.assertEqual(state.mode_manager.current_mode_type, ModeType.RECON)

    def test_axes_default_to_the_autonomy_preset(self):
        state = _state(AUTONOMY_LEVEL=3)
        preset_verbosity, preset_approval = preset_for(3)
        self.assertEqual(state.verbosity_manager.current, preset_verbosity)
        self.assertEqual(state.approval_mode_manager.current, preset_approval)

    def test_explicit_axis_overrides_win_over_preset(self):
        state = _state(AUTONOMY_LEVEL=3, VERBOSITY="silent", APPROVAL_MODE="strict")
        self.assertEqual(state.verbosity_manager.current.value, "silent")
        self.assertEqual(state.approval_mode_manager.current.value, "strict")

    def test_agent_registry_reads_config_budgets(self):
        state = _state(AGENT_MAX_DEPTH=4, AGENT_MAX_TOTAL=7)
        self.assertEqual(state.agent_registry.max_depth, 4)
        self.assertEqual(state.agent_registry.max_total, 7)


if __name__ == "__main__":
    unittest.main()
