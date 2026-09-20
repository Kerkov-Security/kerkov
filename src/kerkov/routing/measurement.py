"""Cumulative engagement measurements and explicit, cache-aware cost estimates.

Rates are USD per million tokens. ``prompt_tokens`` already includes cache reads
and writes; charge only the remainder at the ordinary input rate. No prices are
bundled because a stale price is less useful than an explicitly unknown cost.
"""

import json
import math
from pathlib import Path
from typing import Any, Optional

_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)
_ADDITIVE_FIELDS = (
    "calls",
    "unknown_usage_calls",
    "unpriced_calls",
    "model_seconds",
    "known_cost_usd",
    *_TOKEN_FIELDS,
)


def load_prices(path: Path) -> dict:
    """Load a version-1 rate file, returning its provider/model mapping.

    Format: ``{"version": 1, "models": {"provider/model": {"input": 1,
    "output": 2, "cache_read": 0.1, "cache_write": 1.25}}}``.
    Input/output rates are required; a missing cache rate makes a call's cost
    unknown only when that call used the corresponding cache operation.
    Optional ``as_of`` and ``label`` strings can identify a rate snapshot.
    Invalid configuration raises ValueError; file errors propagate as OSError.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or not isinstance(data.get("models"), dict)
    ):
        raise ValueError("Pricing must have version 1 and a models mapping")
    for field in ("as_of", "label"):
        if field in data and not isinstance(data[field], str):
            raise ValueError(f"Pricing {field} must be a string")
    for name, rates in data["models"].items():
        if not isinstance(name, str) or "/" not in name or not all(name.split("/", 1)):
            raise ValueError("Price keys must be provider/model")
        if not isinstance(rates, dict) or not {"input", "output"} <= rates.keys():
            raise ValueError(f"Pricing for {name} requires input and output rates")
        if rates.keys() - {"input", "output", "cache_read", "cache_write"}:
            raise ValueError(f"Unrecognized rate for {name}")
        for rate in rates.values():
            if not _nonnegative_number(rate):
                raise ValueError(f"Pricing for {name} must be finite and nonnegative")
    return data["models"]


def _nonnegative_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _empty_totals() -> dict:
    return {
        **{field: 0 for field in _ADDITIVE_FIELDS},
        "measurement_complete": True,
        "estimated_cost_usd": 0.0,
        "call_outcomes": {},
    }


def _usage_valid(usage: Any) -> bool:
    return (
        isinstance(usage, dict)
        and all(
            type(usage.get(field)) is int and usage[field] >= 0
            for field in _TOKEN_FIELDS
        )
        and usage["cache_read_tokens"] + usage["cache_creation_tokens"]
        <= usage["prompt_tokens"]
    )


def _call_cost(usage: dict, rates: Optional[dict]) -> Optional[float]:
    if rates is None:
        return None
    portions = {
        "input": usage["prompt_tokens"]
        - usage["cache_read_tokens"]
        - usage["cache_creation_tokens"],
        "output": usage["completion_tokens"],
        "cache_read": usage["cache_read_tokens"],
        "cache_write": usage["cache_creation_tokens"],
    }
    cost = 0.0
    for name, count in portions.items():
        if not count:
            continue
        rate = rates.get(name)
        if not _nonnegative_number(rate):
            return None
        cost += count * rate / 1_000_000
    return cost


def _finish_total(total: dict) -> None:
    total["estimated_cost_usd"] = (
        total["known_cost_usd"]
        if total["measurement_complete"]
        and not total["unknown_usage_calls"]
        and not total["unpriced_calls"]
        else None
    )


def _add_totals(target: dict, source: dict) -> None:
    for field in _ADDITIVE_FIELDS:
        target[field] += source[field]
    target["measurement_complete"] &= source["measurement_complete"]
    for status, count in source["call_outcomes"].items():
        target["call_outcomes"][status] = target["call_outcomes"].get(status, 0) + count


def _read_events(path: Path) -> tuple[list[dict], bool]:
    if not path.exists():
        return [], False
    events = []
    complete = True
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            complete = False
            continue
        if not isinstance(event, dict) or event.get("version") != 1:
            complete = False
            continue
        if event.get("type") == "measurement_gap":
            complete = False
        events.append(event)
    complete &= bool(
        events
        and events[0].get("type") == "measurement_start"
        and events[0].get("complete_history") is True
    )
    return events, complete


def summarize_engagement(
    store_dir: Path, engagement_id: str, prices: Optional[dict] = None
) -> dict:
    """Sum this engagement and descendants, counting each sidecar exactly once.

    ``agents`` includes the requested engagement (usually the main conversation).
    Token counts are known subtotals; missing usage is counted explicitly.
    ``run_seconds`` sums turn/agent durations, including overlapping runs: it is
    cumulative run time, not the engagement's wall-clock duration. Run outcomes
    are reported separately from model-call statuses; completion is not a claim
    that the response is correct.
    """
    store_dir = Path(store_dir)
    metadata = {}
    for path in store_dir.glob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as handle:
                entry = json.loads(handle.readline())
            if entry.get("type") == "metadata" and entry.get("id") == path.stem:
                metadata[path.stem] = entry
        except (OSError, ValueError, AttributeError):
            continue
    if engagement_id not in metadata:
        raise ValueError(f"Engagement not found: {engagement_id}")

    selected = {engagement_id}
    while True:
        descendants = {
            key
            for key, entry in metadata.items()
            if entry.get("parent_engagement_id") in selected
        }
        if descendants <= selected:
            break
        selected.update(descendants)

    totals = {**_empty_totals(), "run_seconds": 0.0, "iterations": 0, "outcomes": {}}
    agents = []
    groups: dict[tuple, dict] = {}
    for agent_id in [engagement_id, *sorted(selected - {engagement_id})]:
        entry = metadata[agent_id]
        events, complete = _read_events(store_dir / "usage" / f"{agent_id}.jsonl")
        agent = {
            **_empty_totals(),
            "engagement_id": agent_id,
            "parent_engagement_id": entry.get("parent_engagement_id"),
            "kind": entry.get("kind", "conversation"),
            "provider": entry.get("provider"),
            "model": entry.get("model"),
            "tier": entry.get("routing_tier"),
            "measurement_complete": complete,
            "run_seconds": 0.0,
            "iterations": 0,
            "outcomes": {},
            "status": "unknown",
            "report_status": None,
        }
        for event in events:
            if event.get("type") == "run_outcome":
                elapsed = event.get("elapsed_seconds")
                iterations = event.get("iterations")
                if (
                    not _nonnegative_number(elapsed)
                    or type(iterations) is not int
                    or iterations < 0
                ):
                    agent["measurement_complete"] = False
                    continue
                status = str(event.get("status", "unknown"))
                agent["run_seconds"] += elapsed
                agent["iterations"] += iterations
                agent["outcomes"][status] = agent["outcomes"].get(status, 0) + 1
                agent["status"] = status
                agent["report_status"] = event.get("report_status")
            elif event.get("type") == "model_call":
                provider = str(event.get("provider", "unknown"))
                model = str(event.get("model", "unknown"))
                tier = event.get("tier")
                group = groups.setdefault(
                    (provider, model, tier),
                    {
                        **_empty_totals(),
                        "provider": provider,
                        "model": model,
                        "tier": tier,
                    },
                )
                call = _empty_totals()
                call["calls"] = 1
                call["call_outcomes"][str(event.get("status", "unknown"))] = 1
                group["measurement_complete"] &= complete
                elapsed = event.get("elapsed_seconds")
                if _nonnegative_number(elapsed):
                    call["model_seconds"] = elapsed
                else:
                    call["measurement_complete"] = False
                usage = event.get("usage")
                if not _usage_valid(usage):
                    call["unknown_usage_calls"] = 1
                else:
                    for field in _TOKEN_FIELDS:
                        call[field] = usage[field]
                    component_total = (
                        usage["prompt_tokens"] + usage["completion_tokens"]
                    )
                    call["total_tokens"] = usage["total_tokens"] or component_total
                    cost = (
                        _call_cost(usage, (prices or {}).get(f"{provider}/{model}"))
                        if call["total_tokens"] == component_total
                        else None
                    )
                    if cost is None:
                        call["unpriced_calls"] = 1
                    else:
                        call["known_cost_usd"] = cost
                _add_totals(agent, call)
                _add_totals(group, call)
        _finish_total(agent)
        _add_totals(totals, agent)
        totals["run_seconds"] += agent["run_seconds"]
        totals["iterations"] += agent["iterations"]
        for status, count in agent["outcomes"].items():
            totals["outcomes"][status] = totals["outcomes"].get(status, 0) + count
        agents.append(agent)

    _finish_total(totals)
    by_model = sorted(
        groups.values(),
        key=lambda row: (row["provider"], row["model"], row["tier"] or ""),
    )
    for group in by_model:
        _finish_total(group)
    return {
        "version": 1,
        "engagement_id": engagement_id,
        "totals": totals,
        "agents": agents,
        "by_model": by_model,
    }


def _format_cost(total: dict) -> str:
    cost = total["estimated_cost_usd"]
    if cost is None:
        return f"unknown (${total['known_cost_usd']:.6f} known subtotal)"
    return f"${cost:.6f}"


def format_summary(summary: dict) -> str:
    """Render a compact plain-text report suitable for the CLI or a saved file."""
    total = summary["totals"]
    lines = [
        f"Usage for {summary['engagement_id']} (engagement and descendants)",
        f"Model calls: {total['calls']} | Unknown usage: {total['unknown_usage_calls']} calls",
        f"Known tokens: {total['prompt_tokens']} input / {total['completion_tokens']} output "
        f"| Cache: {total['cache_read_tokens']} read / {total['cache_creation_tokens']} written",
        f"Model time: {total['model_seconds']:.2f}s | Cumulative run time: {total['run_seconds']:.2f}s "
        "(overlapping runs are summed)",
        f"Estimated cost (USD): {_format_cost(total)}",
    ]
    if not total["measurement_complete"]:
        lines.append(
            "Measurement history is incomplete; earlier or unreadable usage is unknown."
        )
    if total["unpriced_calls"]:
        lines.append(
            f"Unpriced usage: {total['unpriced_calls']} calls "
            "(missing rates or unmatched token totals)"
        )
    lines.append("By provider/model/tier:")
    for row in summary["by_model"]:
        lines.append(
            f"  {row['provider']}/{row['model']} [{row['tier'] or 'unassigned'}]: "
            f"{row['calls']} calls, {row['prompt_tokens']} input / {row['completion_tokens']} output, "
            f"cache {row['cache_read_tokens']} read / {row['cache_creation_tokens']} written, "
            f"{row['model_seconds']:.2f}s, {_format_cost(row)}"
        )
    lines.append("Runs (completion does not establish correctness):")
    for row in summary["agents"]:
        outcomes = ", ".join(
            f"{status}={count}" for status, count in sorted(row["outcomes"].items())
        )
        lines.append(
            f"  {row['engagement_id']} ({row['kind']}): {outcomes or 'no recorded outcome'}, "
            f"{row['calls']} calls, {row['iterations']} iterations, {row['run_seconds']:.2f}s, "
            f"{_format_cost(row)}"
        )
    return "\n".join(lines)
