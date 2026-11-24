"""The shared workflow registry both front ends read."""

import pytest

from kerkov.core.engagement.engagement_restore import (
    EngagementRestoreError,
    restored_workflow,
)
from kerkov.core.session.modes import ModeType
from kerkov.core.workflows.registry import (
    WORKFLOW_DEFINITIONS,
    engagement_workflow,
    engagement_workflow_prompts,
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


def test_ctf_targets_red_for_the_whole_engagement():
    ctf = find_workflow("ctf")
    assert ctf is not None
    assert ctf.mode is ModeType.RED
    assert ctf.actions == ["start", "stop"]
    assert ctf.engagement_scoped
    assert not find_workflow("compliance").engagement_scoped
    # It rides every main turn of the engagement, so it must stay static.
    assert "flag{...}" in ctf.overlay
    assert "{}" not in ctf.overlay and "{0" not in ctf.overlay


def test_only_engagement_scoped_workflows_add_turn_prompts():
    assert engagement_workflow("CTF") is find_workflow("ctf")
    assert engagement_workflow_prompts("ctf") == [find_workflow("ctf").overlay]
    for name in (None, "", "compliance", "recon"):
        assert engagement_workflow(name) is None
        assert engagement_workflow_prompts(name) == []


def test_restoring_an_unknown_engagement_workflow_fails_loudly():
    assert restored_workflow(None, "e1") is None
    assert restored_workflow("ctf", "e1") == "ctf"
    with pytest.raises(EngagementRestoreError, match="'recon'.*Known values: ctf"):
        restored_workflow("recon", "e1")


def test_mode_mismatch_is_advisory_and_names_the_switch():
    compliance = find_workflow("compliance")
    assert mode_mismatch_notice(compliance, ModeType.BLUE) is None
    notice = mode_mismatch_notice(compliance, ModeType.PURPLE)
    assert notice is not None
    assert "Blue" in notice and "Purple" in notice and "/mode blue" in notice
