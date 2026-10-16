"""Cumulative accounting, cache pricing, and engagement-tree persistence."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.engagement.engagement_store import EngagementStore
from kerkov.models.basic_models import Message, TokenUsage
from kerkov.routing.measurement import format_summary, load_prices, summarize_engagement

PRICES = {
    "ExampleProvider/model": {
        "input": 10,
        "output": 20,
        "cache_read": 1,
        "cache_write": 12.5,
    }
}


def _store(path, parent=None, tier=None):
    store = EngagementStore(path)
    store.start_engagement(
        mode="audit",
        model="model",
        verbosity="standard",
        approval_mode="normal",
        provider="ExampleProvider",
        kind="agent" if parent else "conversation",
        parent_engagement_id=parent,
        routing_tier=tier,
    )
    return store


def _call(store, usage=None, status="completed", purpose="completion"):
    store.record_model_call(
        provider="ExampleProvider",
        model="model",
        usage=usage,
        elapsed_seconds=1.5,
        status=status,
        purpose=purpose,
    )


def test_cumulative_tree_includes_main_and_nested_agents_once(tmp_path):
    root = _store(tmp_path)
    child = _store(tmp_path, root.engagement_id, "SMALL")
    grandchild = _store(tmp_path, child.engagement_id, "LARGE")
    unrelated = _store(tmp_path)
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    _call(root, usage)
    _call(root, usage, purpose="compaction")
    _call(child, usage)
    _call(grandchild, usage)
    _call(unrelated, usage)
    for store, status in [
        (root, "completed"),
        (child, "failed"),
        (grandchild, "cancelled"),
    ]:
        store.record_run_outcome(
            status=status,
            elapsed_seconds=5,
            iterations=2,
            report_status="partial" if status == "failed" else None,
        )

    summary = summarize_engagement(tmp_path, root.engagement_id, PRICES)
    totals = summary["totals"]
    assert totals["calls"] == 4
    assert totals["prompt_tokens"] == 400
    assert totals["completion_tokens"] == 80
    assert totals["total_tokens"] == 480
    assert totals["model_seconds"] == 6
    assert totals["run_seconds"] == 15
    assert totals["iterations"] == 6
    assert totals["outcomes"] == {"completed": 1, "failed": 1, "cancelled": 1}
    assert totals["estimated_cost_usd"] == pytest.approx(0.0056)
    assert len(summary["agents"]) == 3
    assert {group["tier"] for group in summary["by_model"]} == {None, "SMALL", "LARGE"}
    assert sum(group["calls"] for group in summary["by_model"]) == 4
    child_summary = summarize_engagement(tmp_path, child.engagement_id, PRICES)
    assert child_summary["totals"]["calls"] == 2
    assert child_summary["agents"][0]["report_status"] == "partial"


def test_cache_counts_are_subtracted_from_input_before_pricing(tmp_path):
    store = _store(tmp_path)
    _call(
        store,
        TokenUsage(
            prompt_tokens=1000,
            completion_tokens=100,
            total_tokens=1100,
            cache_read_tokens=600,
            cache_creation_tokens=200,
        ),
    )
    totals = summarize_engagement(tmp_path, store.engagement_id, PRICES)["totals"]
    # 200 ordinary input + 600 cache reads + 200 cache writes + 100 output.
    assert totals["estimated_cost_usd"] == pytest.approx(0.0071)
    assert totals["prompt_tokens"] == 1000
    assert totals["cache_read_tokens"] == 600
    assert totals["cache_creation_tokens"] == 200


def test_unknown_usage_keeps_known_subtotal_without_claiming_total(tmp_path):
    store = _store(tmp_path)
    _call(store, TokenUsage(prompt_tokens=100, completion_tokens=50))
    _call(store, status="failed")
    _call(store, status="cancelled")
    summary = summarize_engagement(tmp_path, store.engagement_id, PRICES)
    totals = summary["totals"]
    assert totals["calls"] == 3
    assert totals["unknown_usage_calls"] == 2
    assert totals["total_tokens"] == 150
    assert totals["estimated_cost_usd"] is None
    assert totals["known_cost_usd"] == pytest.approx(0.002)
    assert totals["call_outcomes"] == {"completed": 1, "failed": 1, "cancelled": 1}
    assert "known subtotal" in format_summary(summary)


def test_provider_total_with_unclassified_tokens_is_preserved_and_unpriced(tmp_path):
    store = _store(tmp_path)
    _call(
        store,
        TokenUsage(
            prompt_tokens=100,
            completion_tokens=20,
            cache_read_tokens=30,
            total_tokens=150,
        ),
    )
    summary = summarize_engagement(tmp_path, store.engagement_id, PRICES)
    for totals in [summary["totals"], summary["agents"][0], summary["by_model"][0]]:
        assert totals["total_tokens"] == 150
        assert totals["prompt_tokens"] == 100
        assert totals["completion_tokens"] == 20
        assert totals["cache_read_tokens"] == 30
        assert totals["estimated_cost_usd"] is None
        assert totals["unpriced_calls"] == 1
    assert "unmatched token totals" in format_summary(summary)


def test_missing_model_and_cache_prices_stay_unknown(tmp_path):
    store = _store(tmp_path)
    _call(store, TokenUsage(prompt_tokens=100, cache_read_tokens=80))
    for prices in [None, {}, {"ExampleProvider/model": {"input": 10, "output": 20}}]:
        totals = summarize_engagement(tmp_path, store.engagement_id, prices)["totals"]
        assert totals["estimated_cost_usd"] is None
        assert totals["unpriced_calls"] == 1
        assert totals["unknown_usage_calls"] == 0


def test_zero_rates_and_known_zero_usage_are_not_unknown(tmp_path):
    store = _store(tmp_path)
    _call(store, TokenUsage(prompt_tokens=20, completion_tokens=10))
    _call(store, TokenUsage())
    prices = {"ExampleProvider/model": {"input": 0, "output": 0}}
    totals = summarize_engagement(tmp_path, store.engagement_id, prices)["totals"]
    assert totals["estimated_cost_usd"] == 0
    assert totals["unknown_usage_calls"] == 0
    assert totals["unpriced_calls"] == 0
    # Unknown prices remain unknown even when a provider reports zero tokens.
    zero = _store(tmp_path)
    _call(zero, TokenUsage())
    assert (
        summarize_engagement(tmp_path, zero.engagement_id)["totals"][
            "estimated_cost_usd"
        ]
        is None
    )


def test_compaction_and_resume_preserve_measurements_and_tier(tmp_path):
    store = _store(tmp_path, parent="parent", tier="MEDIUM")
    store.append_message(Message(role="user", content="original prompt"))
    _call(store, TokenUsage(prompt_tokens=100), purpose="compaction")
    store.override_engagement_messages([Message(role="assistant", content="summary")])
    resumed = EngagementStore(tmp_path)
    engagement = resumed.load_engagement(store.engagement_id)
    assert [message.content for message in engagement.messages] == ["summary"]
    _call(resumed, TokenUsage(prompt_tokens=50))
    summary = summarize_engagement(tmp_path, store.engagement_id, PRICES)
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["prompt_tokens"] == 150
    assert summary["totals"]["measurement_complete"] is True
    assert summary["by_model"][0]["tier"] == "MEDIUM"
    text = (tmp_path / "usage" / f"{store.engagement_id}.jsonl").read_text()
    assert "original prompt" not in text
    assert "summary" not in text
    assert '"purpose": "compaction"' in text


def test_legacy_history_is_unknown_before_and_after_recording(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "usage" / f"{store.engagement_id}.jsonl").unlink()
    legacy = summarize_engagement(tmp_path, store.engagement_id, PRICES)
    assert legacy["totals"]["measurement_complete"] is False
    assert legacy["totals"]["estimated_cost_usd"] is None
    assert legacy["totals"]["calls"] == 0
    resumed = EngagementStore(tmp_path)
    assert resumed.load_engagement(store.engagement_id) is not None
    _call(resumed, TokenUsage(prompt_tokens=100))
    totals = summarize_engagement(tmp_path, store.engagement_id, PRICES)["totals"]
    assert totals["measurement_complete"] is False
    assert totals["estimated_cost_usd"] is None
    assert totals["known_cost_usd"] == pytest.approx(0.001)


def test_unmeasured_child_prevents_complete_engagement_estimate(tmp_path):
    root = _store(tmp_path)
    child = _store(tmp_path, root.engagement_id)
    _call(root, TokenUsage(prompt_tokens=100))
    (tmp_path / "usage" / f"{child.engagement_id}.jsonl").unlink()
    totals = summarize_engagement(tmp_path, root.engagement_id, PRICES)["totals"]
    assert totals["estimated_cost_usd"] is None
    assert totals["known_cost_usd"] == pytest.approx(0.001)


def test_truncated_sidecar_keeps_known_measurements_but_marks_incomplete(tmp_path):
    store = _store(tmp_path)
    _call(store, TokenUsage(prompt_tokens=100))
    path = tmp_path / "usage" / f"{store.engagement_id}.jsonl"
    with path.open("a") as handle:
        handle.write('{"type":"model_call"')
    summary = summarize_engagement(tmp_path, store.engagement_id, PRICES)
    assert summary["totals"]["calls"] == 1
    assert summary["totals"]["estimated_cost_usd"] is None
    assert "history is incomplete" in format_summary(summary)


def test_recovered_write_marks_missing_call_instead_of_claiming_full_cost(tmp_path):
    store = _store(tmp_path)
    _call(store, TokenUsage(prompt_tokens=100))
    # Logging may use open elsewhere, so fail only the measurement sidecar.
    usage_path = tmp_path / "usage" / f"{store.engagement_id}.jsonl"
    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path == usage_path:
            raise OSError("temporarily unavailable")
        return original_open(path, *args, **kwargs)

    with patch.object(Path, "open", failing_open):
        _call(store, TokenUsage(prompt_tokens=200))
    store.record_run_outcome(status="completed", elapsed_seconds=3, iterations=2)
    _call(store, TokenUsage(prompt_tokens=300))

    summary = summarize_engagement(tmp_path, store.engagement_id, PRICES)
    totals = summary["totals"]
    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 400
    assert totals["measurement_complete"] is False
    assert totals["estimated_cost_usd"] is None
    assert totals["known_cost_usd"] == pytest.approx(0.004)
    assert summary["by_model"][0]["estimated_cost_usd"] is None
    events = [json.loads(line) for line in usage_path.read_text().splitlines()]
    assert sum(event["type"] == "measurement_gap" for event in events) == 1


def test_delete_removes_sidecar(tmp_path):
    store = _store(tmp_path)
    path = tmp_path / "usage" / f"{store.engagement_id}.jsonl"
    assert path.exists()
    assert store.delete_engagement(store.engagement_id)
    assert not path.exists()


def test_pricing_file_version_and_optional_snapshot_metadata(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {"version": 1, "as_of": "2026-09-20", "label": "test", "models": PRICES}
        )
    )
    assert load_prices(path) == PRICES


@pytest.mark.parametrize("recorded_provider", ["anthropic", "AnthropicProvider"])
@pytest.mark.parametrize("pricing_provider", ["anthropic", "AnthropicProvider"])
def test_canonical_and_legacy_provider_names_share_prices(
    tmp_path, recorded_provider, pricing_provider
):
    store = EngagementStore(tmp_path)
    store.start_engagement(
        mode="audit",
        model="vendor/model",
        provider=recorded_provider,
        verbosity="standard",
        approval_mode="normal",
    )
    for provider in [recorded_provider, "anthropic"]:
        store.record_model_call(
            provider=provider,
            model="vendor/model",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20),
            elapsed_seconds=1,
            status="completed",
        )
    prices = {f"{pricing_provider}/vendor/model": {"input": 10, "output": 20}}
    summary = summarize_engagement(tmp_path, store.engagement_id, prices)
    assert summary["totals"]["estimated_cost_usd"] == pytest.approx(0.0028)
    assert summary["agents"][0]["provider"] == "anthropic"
    assert len(summary["by_model"]) == 1
    assert summary["by_model"][0]["provider"] == "anthropic"
    assert summary["by_model"][0]["calls"] == 2


def test_price_file_normalizes_legacy_names_and_rejects_conflicting_aliases(tmp_path):
    path = tmp_path / "prices.json"
    rates = {"input": 1, "output": 2}
    models = {"AnthropicProvider/model": rates, "anthropic/model": rates}
    path.write_text(json.dumps({"version": 1, "models": models}))
    assert load_prices(path) == {"anthropic/model": rates}
    models["anthropic/model"] = {"input": 3, "output": 4}
    path.write_text(json.dumps({"version": 1, "models": models}))
    with pytest.raises(
        ValueError, match="Conflicting pricing aliases for anthropic/model"
    ):
        load_prices(path)


@pytest.mark.parametrize(
    "data",
    [
        {"version": 2, "models": PRICES},
        {"version": True, "models": PRICES},
        {"version": 1, "models": []},
        {"version": 1, "models": {"model": {"input": 1, "output": 2}}},
        {"version": 1, "models": {"provider/model": {"input": 1}}},
        {"version": 1, "models": {"provider/model": {"input": -1, "output": 2}}},
        {
            "version": 1,
            "models": {"provider/model": {"input": float("inf"), "output": 2}},
        },
        {"version": 1, "models": {"provider/model": {"input": True, "output": 2}}},
        {
            "version": 1,
            "models": {"provider/model": {"input": 1, "output": 2, "cache_reads": 3}},
        },
    ],
)
def test_invalid_prices_are_rejected(tmp_path, data):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_prices(path)


def test_missing_engagement_does_not_report_zero_cost(tmp_path):
    with pytest.raises(ValueError, match="Engagement not found"):
        summarize_engagement(tmp_path, "missing")
