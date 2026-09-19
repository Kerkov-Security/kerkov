"""Conservative routing must require every cheap-tier condition."""

from dataclasses import FrozenInstanceError, replace

import pytest

from kerkov.routing import RoutingPolicy, TaskRoutingContext


@pytest.fixture
def context() -> TaskRoutingContext:
    return TaskRoutingContext(
        task_kind="extract",
        profile="analysis",
        instruction="Extract the listed host names from the supplied text.",
        success_criteria="Return one host name per line, preserving the original order.",
        tools=("read", "recall"),
        trusted_profile=True,
    )


@pytest.mark.parametrize("kind", ["extract", "format"])
def test_explicit_bounded_tasks_can_use_cheap_tier(context, kind) -> None:
    decision = RoutingPolicy("small", "large").select(replace(context, task_kind=kind))

    assert decision.model == "small"
    assert decision.tier == "cheap"
    assert kind in decision.reason
    with pytest.raises(FrozenInstanceError):
        decision.model = "other"


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"task_kind": "general"}, "explicitly eligible"),
        ({"task_kind": "summarize"}, "explicitly eligible"),
        ({"profile": "recon"}, "built-in analysis"),
        ({"trusted_profile": False}, "built-in analysis"),
        ({"tools": ("read", "exec")}, "Effective tools"),
        ({"tools": ("delegate",)}, "Effective tools"),
        ({"tools": ("web_search",)}, "Effective tools"),
        ({"instruction": " "}, "bounded"),
        ({"success_criteria": ""}, "bounded"),
        ({"instruction": "x" * 2001}, "bounded"),
        ({"success_criteria": "x" * 1001}, "bounded"),
        ({"instruction": " " * 2000 + "Extract names."}, "bounded"),
        ({"success_criteria": " " * 1000 + "Return the list."}, "bounded"),
        ({"instruction": "Format a SQL injection payload."}, "security decisions"),
        (
            {"instruction": "Extract the scope and decide authorization."},
            "security decisions",
        ),
        (
            {"instruction": "Extract and prioritize vulnerabilities."},
            "security decisions",
        ),
        (
            {"success_criteria": "Assess which passwords are valid."},
            "security decisions",
        ),
    ],
)
def test_uncertain_or_capable_tasks_use_strong_tier(context, overrides, reason) -> None:
    decision = RoutingPolicy("small", "large").select(replace(context, **overrides))

    assert decision.model == "large"
    assert decision.tier == "strong"
    assert reason in decision.reason


@pytest.mark.parametrize(
    "cheap,strong",
    [("", "large"), ("small", ""), ("small", "small"), (" small", "large")],
)
def test_invalid_model_pair_is_rejected(cheap, strong) -> None:
    with pytest.raises(ValueError):
        RoutingPolicy(cheap, strong)
