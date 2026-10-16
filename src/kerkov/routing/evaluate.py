"""Opt-in, fixture-only comparison of production routing and forced LARGE.

Run ``python -m kerkov.routing.evaluate --list`` without making model calls.
"""

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Optional

from kerkov.agents.agent_runner import AgentRunner
from kerkov.agents.agent_session import AgentSession
from kerkov.agents.agent_tool import AgentTaskSpec
from kerkov.agents.base_agent import BUILTIN_PROFILES
from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.core.config_manager import get_config
from kerkov.core.knowledge_base.engagement_kb import EngagementKnowledgeBase
from kerkov.providers.base_provider import BaseLLMProvider, provider_name
from kerkov.providers.provider_manager import ProviderManager
from kerkov.routing.policy import RoutingDecision, RoutingPolicy, TaskRoutingContext
from kerkov.tools.recall_tool import RecallTool

RUN_SETTINGS = {"max_iterations": 4, "max_response_tokens": 2048, "temperature": 0.0}
TIMEOUT_SECONDS = 180
ALLOWED_TOOLS = ("recall",)


def load_cases() -> list[dict[str, Any]]:
    return json.loads(
        files("kerkov.routing").joinpath("evaluation_cases.json").read_text("utf-8")
    )


def task_spec(case: dict[str, Any]) -> AgentTaskSpec:
    """Use a valid, explicit delegate override rather than profile defaults."""
    return AgentTaskSpec(
        agent_type="analysis",
        instruction=case["instruction"],
        success_criteria=case["success_criteria"],
        task_kind=case["task_kind"],
        allowed_tools=list(ALLOWED_TOOLS),
    )


def select_route(case: dict[str, Any], policy: RoutingPolicy) -> RoutingDecision:
    task = task_spec(case)
    profile = BUILTIN_PROFILES[task.agent_type]
    return policy.select(
        TaskRoutingContext(
            task_kind=task.task_kind,
            profile=profile.name,
            instruction=task.instruction,
            success_criteria=task.success_criteria,
            tools=tuple(profile.effective_tools(task.allowed_tools)),
            trusted_profile=profile.source == "builtin",
        )
    )


def grade(case: dict[str, Any], status: str, findings: str) -> dict[str, str]:
    """The runner's completion status is necessary, never sufficient."""
    if status != "completed":
        return {"status": "fail", "reason": f"Run did not complete ({status})."}
    check = case["check"]
    if check["type"] == "rubric":
        return {"status": "pending_review", "reason": check["rubric"]}
    try:
        actual = json.loads(findings)
    except (ValueError, TypeError):
        return {"status": "fail", "reason": "Findings are not a JSON value."}
    # Canonical JSON also distinguishes booleans from integers and rejects extra
    # fields; Python equality alone would accept True as an expected 1.
    if json.dumps(actual, sort_keys=True) != json.dumps(
        check["expected"], sort_keys=True
    ):
        return {"status": "fail", "reason": "JSON differs from the frozen answer."}
    return {"status": "pass", "reason": "Exact JSON matches the frozen answer."}


async def run_arm(
    case: dict[str, Any],
    arm: str,
    decision: RoutingDecision,
    provider: BaseLLMProvider,
    prices: Optional[dict] = None,
) -> dict[str, Any]:
    from kerkov.routing.measurement import summarize_engagement

    started = time.monotonic()
    findings, report_status, error = "", "failed", None
    summary = None
    with tempfile.TemporaryDirectory(prefix="kerkov-evaluation-") as directory:
        root = Path(directory)
        store = EngagementStore(store_dir=root / "engagements")
        session = AgentSession(
            id=case["id"],
            agent_run_id=f"{case['id']}-{arm}",
            profile="analysis",
            task=case["instruction"],
            routing_tier=decision.tier,
            routing_reason=decision.reason,
        )
        with closing(EngagementKnowledgeBase("evaluation", root / "knowledge")) as kb:
            try:
                task = task_spec(case)
                runner = AgentRunner(
                    profile=BUILTIN_PROFILES[task.agent_type],
                    instruction=task.instruction,
                    success_criteria=task.success_criteria,
                    provider=provider.fork(decision.model),
                    tools=[RecallTool(lambda: kb)],
                    engagement_store=store,
                    knowledge_base=kb,
                    session=session,
                    run_workspace=root / "workspace",
                    **RUN_SETTINGS,
                )
                report = await asyncio.wait_for(runner.run(), TIMEOUT_SECONDS)
                findings, report_status = report.findings, report.status
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if store.engagement_id:
            summary = summarize_engagement(store.store_dir, store.engagement_id, prices)
    return {
        "case_id": case["id"],
        "category": case["category"],
        "arm": arm,
        "model": decision.model,
        "tier": decision.tier,
        "routing_reason": decision.reason,
        "report_status": report_status,
        "correctness": grade(case, report_status, findings),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "findings": findings,
        "error": error,
        "usage": summary["totals"] if summary else None,
    }


def _totals(results: list[dict[str, Any]]) -> dict[str, Any]:
    usage = [r["usage"] for r in results]
    complete = all(u and u["measurement_complete"] for u in usage)
    cost_known = all(u and u["estimated_cost_usd"] is not None for u in usage)
    # Keep a null for unmeasured arms, and coverage alongside partial totals.
    totals = {
        "cases": len(results),
        "correctness": dict(Counter(r["correctness"]["status"] for r in results)),
        "routes": dict(Counter(r["tier"] for r in results)),
        "elapsed_seconds": sum(r["elapsed_seconds"] for r in results),
        "measurement_complete": complete,
        "token_usage_complete": complete
        and all(not u["unknown_usage_calls"] for u in usage if u),
        "unmeasured_runs": sum(u is None for u in usage),
        "estimated_cost_usd": (
            sum(u["estimated_cost_usd"] for u in usage) if cost_known else None
        ),
    }
    for key in (
        "calls",
        "unknown_usage_calls",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        totals[key] = sum(u[key] for u in usage if u) if any(usage) else None
    return totals


async def evaluate(
    cases: list[dict[str, Any]],
    provider: BaseLLMProvider,
    policy: RoutingPolicy,
    prices: Optional[dict] = None,
) -> dict[str, Any]:
    """Compare each fixture twice, alternating arm order; no retries or grader."""
    results = []
    for index, case in enumerate(cases):
        arms = [
            ("auto", select_route(case, policy)),
            ("large", RoutingDecision(policy.large_model, "large", "Forced baseline.")),
        ]
        if index % 2:
            arms.reverse()
        for arm, decision in arms:
            results.append(await run_arm(case, arm, decision, provider, prices))
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider_name(provider),
        "models": {
            "small": policy.small_model,
            "medium": policy.medium_model,
            "large": policy.large_model,
        },
        "settings": {**RUN_SETTINGS, "timeout_seconds": TIMEOUT_SECONDS},
        "prices": prices,
        "tools": [*ALLOWED_TOOLS, "report"],
        "delegate_assumptions": {
            "agent_type": "analysis",
            "allowed_tools": list(ALLOWED_TOOLS),
            "effective_tools": BUILTIN_PROFILES["analysis"].effective_tools(
                list(ALLOWED_TOOLS)
            ),
            "knowledge_base": "Isolated temporary store; initially empty.",
            "nested_delegation": False,
        },
        "profile": "analysis (builtin)",
        "system_prompt_sha256": hashlib.sha256(
            BUILTIN_PROFILES["analysis"].system_prompt.encode()
        ).hexdigest(),
        "corpus_sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True).encode()
        ).hexdigest(),
        "cases": cases,
        "summary": {
            arm: _totals([r for r in results if r["arm"] == arm])
            for arm in ("auto", "large")
        },
        "results": results,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = ["Routing evaluation (estimated USD; pending review is not a pass)"]
    for arm, summary in report["summary"].items():
        cost = summary["estimated_cost_usd"]
        cost_text = "unknown" if cost is None else f"${cost:.6f}"
        coverage = "complete" if summary["token_usage_complete"] else "unknown/partial"
        tokens = f"{summary['prompt_tokens']}/{summary['completion_tokens']}"
        cache = f"{summary['cache_read_tokens']}/{summary['cache_creation_tokens']}"
        if not summary["token_usage_complete"]:
            tokens = f"unknown (known subtotal {tokens})"
            cache = f"unknown (known subtotal {cache})"
        lines.append(
            f"{arm}: {summary['correctness']}; routes={summary['routes']}; "
            f"elapsed={summary['elapsed_seconds']:.2f}s; "
            f"calls={summary['calls']}; "
            f"input/output={tokens}; cache read/write={cache}; "
            f"cost={cost_text}; usage={coverage}"
        )
    for result in report["results"]:
        lines.append(
            f"  {result['case_id']} [{result['arm']}/{result['tier']}]: "
            f"{result['correctness']['status']} ({result['report_status']})"
            + (f" — {result['error']}" if result["error"] else "")
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="List fixtures; no API calls"
    )
    parser.add_argument(
        "--case", action="append", dest="case_ids", help="Run this ID only (repeatable)"
    )
    parser.add_argument("--output", type=Path, help="Write the full JSON result here")
    parser.add_argument("--prices", type=Path, help="Optional model price JSON")
    args = parser.parse_args(argv)
    cases = load_cases()
    if args.case_ids:
        unknown = set(args.case_ids) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"Unknown case IDs: {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case["id"] in args.case_ids]
    if args.list:
        for case in cases:
            print(f"{case['id']}: {case['category']} ({case['check']['type']})")
        return 0
    if not args.output:
        parser.error("--output is required for a live evaluation (or use --list)")
    from kerkov.routing.measurement import load_prices

    try:
        prices = load_prices(args.prices) if args.prices else None
        manager = ProviderManager(get_config())
        manager.initialize_provider()
        policy = manager.enable_agent_routing()
        if manager.provider is None:
            raise ValueError("No provider is configured.")
        report = asyncio.run(evaluate(cases, manager.provider, policy, prices))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(format_report(report))
    print(f"Full results: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
