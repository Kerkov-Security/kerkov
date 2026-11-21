"""The shared workflow registry both front ends read."""

from kerkov.core.session.modes import ModeType
from kerkov.core.workflows.registry import (
    WORKFLOW_DEFINITIONS,
    find_workflow,
    mode_mismatch_notice,
    workflow_names,
)


def test_definitions_are_uniquely_named_and_declare_a_team_role():
    assert workflow_names() == sorted(set(workflow_names()), key=workflow_names().index)
    for definition in WORKFLOW_DEFINITIONS:
        assert definition.name == definition.name.lower()
        assert isinstance(definition.mode, ModeType)
        assert definition.overlay.strip()
        assert definition.actions


def test_lookup_is_case_insensitive_and_rejects_unknown_names():
    assert find_workflow("Compliance") is find_workflow("compliance")
    assert find_workflow("  COMPLIANCE ") is not None
    assert find_workflow("recon") is None


def test_compliance_targets_blue_and_its_overlay_carries_no_per_run_data():
    compliance = find_workflow("compliance")
    assert compliance is not None
    assert compliance.mode is ModeType.BLUE
    assert compliance.actions == ["assess", "preview", "apply"]
    # The overlay is a trailing system message on every turn of the run, so a
    # host, engagement ID or timestamp in it would break prompt caching.
    assert "{" not in compliance.overlay
    assert compliance.overlay == find_workflow("compliance").overlay


def test_mode_mismatch_is_advisory_and_names_the_switch():
    compliance = find_workflow("compliance")
    assert mode_mismatch_notice(compliance, ModeType.BLUE) is None
    notice = mode_mismatch_notice(compliance, ModeType.PURPLE)
    assert notice is not None
    assert "Blue" in notice and "Purple" in notice and "/mode blue" in notice
