"""Fixture evaluation tests run real agent loops with an in-memory provider."""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

from kerkov.models.basic_models import LLMResponse, TokenUsage, ToolCall
from kerkov.providers.base_provider import BaseLLMProvider
from kerkov.routing.evaluate import (
    evaluate,
    format_report,
    grade,
    load_cases,
    main,
    select_route,
)
from kerkov.routing.policy import RoutingPolicy

POLICY = RoutingPolicy("small", "medium", "large")


class FixtureProvider(BaseLLMProvider):
    def __init__(
        self, case, *, findings=None, status="completed", usage=True, error=None
    ):
        super().__init__("fixture", "main")
        self.backend = SimpleNamespace(requests=[])
        self.findings = (
            json.dumps(case["check"].get("expected")) if findings is None else findings
        )
        self.status = status
        self.usage = usage
        self.error = error

    async def complete(
        self, messages, temperature=None, max_tokens=None, tools=None, on_delta=None
    ):
        self.backend.requests.append(
            {
                "model": self.model,
                "messages": [(m.role, m.content) for m in messages],
                "tools": [t.name for t in tools],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.error and self.model == "small":
            raise self.error
        return LLMResponse(
            content="",
            model=self.model,
            usage=(
                TokenUsage(
                    prompt_tokens=100,
                    completion_tokens=10,
                    total_tokens=110,
                    cache_read_tokens=20,
                    cache_creation_tokens=10,
                )
                if self.usage
                else None
            ),
            tool_calls=[
                ToolCall(
                    tool_name="report",
                    call_id="answer",
                    parameters={
                        "status": self.status,
                        "findings": self.findings,
                    },
                )
            ],
        )

    def validate_connection(self):
        return True

    def list_models(self):
        return ["small", "medium", "large"]

    async def get_max_context_tokens(self):
        return 32000

    async def get_max_output_tokens(self):
        return 2048


def test_corpus_exercises_all_tiers_and_has_independent_checks():
    cases = load_cases()
    assert len(cases) == len({c["id"] for c in cases}) == 24
    assert Counter(select_route(case, POLICY).tier for case in cases) == {
        "small": 8,
        "medium": 8,
        "large": 8,
    }
    assert Counter(c["check"]["type"] for c in cases) == {"json": 20, "rubric": 4}
    for case in cases:
        if case["check"]["type"] == "json":
            assert (
                grade(case, "completed", json.dumps(case["check"]["expected"]))[
                    "status"
                ]
                == "pass"
            )
            assert grade(case, "completed", "null")["status"] == "fail"


def test_both_arms_have_identical_inputs_tools_and_settings():
    case = load_cases()[0]
    provider = FixtureProvider(case)
    report = asyncio.run(evaluate([case], provider, POLICY))
    auto, large = provider.backend.requests
    assert auto.pop("model") == "small"
    assert large.pop("model") == "large"
    assert auto == large
    assert auto["tools"] == ["report"]
    assert provider.model == "main"
    assert [r["correctness"]["status"] for r in report["results"]] == ["pass", "pass"]


def test_completed_wrong_answer_and_noncompleted_correct_answer_fail():
    case = load_cases()[0]
    for provider in [
        FixtureProvider(case, findings='["wrong.lab"]'),
        FixtureProvider(case, status="partial"),
        FixtureProvider(case, status="stuck"),
    ]:
        report = asyncio.run(evaluate([case], provider, POLICY))
        assert [r["correctness"]["status"] for r in report["results"]] == [
            "fail",
            "fail",
        ]
    numeric = {"check": {"type": "json", "expected": {"count": 1}}}
    assert grade(numeric, "completed", '{"count": true}')["status"] == "fail"
    assert grade(numeric, "completed", '{"count": 1, "extra": 0}')["status"] == "fail"


def test_rubrics_are_pending_and_errors_do_not_prevent_baseline():
    manual = load_cases()[-1]
    assert grade(manual, "completed", "Plausible prose")["status"] == "pending_review"
    assert grade(manual, "partial", "Plausible prose")["status"] == "fail"
    case = load_cases()[0]
    report = asyncio.run(
        evaluate(
            [case], FixtureProvider(case, error=RuntimeError("fixture failure")), POLICY
        )
    )
    assert report["results"][0]["correctness"]["status"] == "fail"
    assert report["results"][0]["error"] == "RuntimeError: fixture failure"
    assert report["results"][1]["correctness"]["status"] == "pass"


def test_usage_and_cache_aware_cost_use_actual_measurements():
    case = load_cases()[0]
    provider = FixtureProvider(case)
    rates = {
        f"FixtureProvider/{model}": {
            "input": 1,
            "output": 2,
            "cache_read": 0.1,
            "cache_write": 1.25,
        }
        for model in ("small", "medium", "large")
    }
    report = asyncio.run(evaluate([case], provider, POLICY, rates))
    for summary in report["summary"].values():
        assert summary["calls"] == 1
        assert summary["prompt_tokens"] == 100
        assert summary["completion_tokens"] == 10
        assert summary["cache_read_tokens"] == 20
        assert summary["cache_creation_tokens"] == 10
        assert abs(summary["estimated_cost_usd"] - 0.0001045) < 1e-12
        assert summary["token_usage_complete"]


def test_missing_usage_and_missing_prices_remain_unknown():
    case = load_cases()[0]
    for provider in [FixtureProvider(case, usage=False), FixtureProvider(case)]:
        report = asyncio.run(evaluate([case], provider, POLICY))
        for summary in report["summary"].values():
            assert summary["estimated_cost_usd"] is None
        assert "cost=unknown" in format_report(report)
        if not provider.usage:
            assert report["summary"]["auto"]["unknown_usage_calls"] == 1
            assert not report["summary"]["auto"]["token_usage_complete"]
            assert "usage=unknown/partial" in format_report(report)


def test_list_does_not_initialize_provider(capsys):
    with patch("kerkov.routing.evaluate.ProviderManager") as manager:
        assert main(["--list", "--case", "inventory-hosts"]) == 0
        manager.assert_not_called()
    assert "inventory-hosts" in capsys.readouterr().out
