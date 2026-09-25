"""Conservative routing must require every condition for small and medium tasks."""

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
def test_explicit_bounded_tasks_can_use_small_tier(context, kind) -> None:
    decision = RoutingPolicy("small", "medium", "large").select(
        replace(context, task_kind=kind)
    )

    assert decision.model == "small"
    assert decision.tier == "small"
    assert kind in decision.reason
    with pytest.raises(FrozenInstanceError):
        decision.model = "other"


@pytest.mark.parametrize(
    "instruction",
    [
        "Compare the two supplied host inventories and explain their differences.",
        "Analyze the provided counts and recommend the clearest table layout.",
        "Explain the differences in these two excerpts. " + "x" * 2500,
    ],
    ids=["compare-inventories", "recommend-layout", "longer-bounded-analysis"],
)
def test_bounded_routine_analysis_can_use_medium_tier(context, instruction) -> None:
    decision = RoutingPolicy("small", "medium", "large").select(
        replace(context, task_kind="analyze", instruction=instruction)
    )

    assert decision.model == "medium"
    assert decision.tier == "medium"
    assert "analyze" in decision.reason


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"task_kind": "general"}, "explicitly eligible"),
        ({"task_kind": "compare"}, "explicitly eligible"),
        ({"profile": "recon"}, "built-in analysis"),
        ({"trusted_profile": False}, "built-in analysis"),
        ({"tools": ("read", "exec")}, "Effective tools"),
        ({"tools": ("read", "delegate")}, "Effective tools"),
        ({"instruction": " "}, "bounded"),
        ({"success_criteria": ""}, "bounded"),
        ({"instruction": "x" * 4001}, "bounded"),
        ({"success_criteria": "x" * 1001}, "bounded"),
        ({"instruction": " " * 4000 + "Compare the lists."}, "bounded"),
        ({"success_criteria": " " * 1000 + "Return a table."}, "bounded"),
        ({"instruction": "Compare the security risks."}, "security decisions"),
        ({"success_criteria": "Choose the best exploit."}, "security decisions"),
        ({"instruction": "Analyze and execute the command."}, "security decisions"),
        (
            {"instruction": "Explain the root cause of the failure."},
            "complex reasoning",
        ),
        ({"instruction": "Prove this theorem."}, "complex reasoning"),
        ({"success_criteria": "Include a multi-step diagnosis."}, "complex reasoning"),
    ],
)
def test_analysis_requires_every_medium_tier_condition(
    context, overrides, reason
) -> None:
    context = replace(context, task_kind="analyze")
    decision = RoutingPolicy("small", "medium", "large").select(
        replace(context, **overrides)
    )

    assert decision.model == "large"
    assert decision.tier == "large"
    assert reason in decision.reason


@pytest.mark.parametrize("field", ["instruction", "success_criteria"])
@pytest.mark.parametrize(
    "text",
    [
        "Assess CVE-2026-1234 applicability to version 1.2.",
        "Compare SQLi and XSS findings.",
        "Explain whether authentication is required.",
        "Assess shell and root access.",
        "Compare potential SSRF and RCE paths.",
        "Assess applicability of these CVEs.",
        "Compare these CWEs.",
        "Explain the listed RCEs.",
        "Extract the CVEs from the supplied text.",
    ],
)
@pytest.mark.parametrize("kind", ["extract", "format", "analyze"])
def test_security_topics_use_large_in_instruction_or_criteria(
    context, field, text, kind
) -> None:
    context = replace(context, task_kind=kind, **{field: text})

    decision = RoutingPolicy("small", "medium", "large").select(context)

    assert decision.tier == "large"
    assert "security decisions" in decision.reason


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
        ({"instruction": "Compare and explain the host lists."}, "reasoning beyond"),
        ({"instruction": "Format a complex algorithm."}, "complex reasoning"),
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
def test_uncertain_or_capable_tasks_use_large_tier(context, overrides, reason) -> None:
    decision = RoutingPolicy("small", "medium", "large").select(
        replace(context, **overrides)
    )

    assert decision.model == "large"
    assert decision.tier == "large"
    assert reason in decision.reason


@pytest.mark.parametrize(
    "small,medium,large",
    [
        ("", "medium", "large"),
        ("small", "", "large"),
        ("small", "medium", ""),
        ("small", "small", "large"),
        ("small", "medium", "small"),
        ("small", "medium", "medium"),
        (" small", "medium", "large"),
        ("small", "medium tier", "large"),
    ],
)
def test_invalid_model_tiers_are_rejected(small, medium, large) -> None:
    with pytest.raises(ValueError):
        RoutingPolicy(small, medium, large)
