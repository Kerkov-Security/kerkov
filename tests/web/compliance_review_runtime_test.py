"""Local control and export requests stay bound to their saved engagement."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kerkov.core.compliance.review import export_report
from kerkov.core.compliance.workflows import WorkflowStore
from kerkov.web.console.consumers import ConsoleConsumer
from kerkov.web.runtime.compliance import review_saved_run
from kerkov.web.runtime.events import WebEventBus
from tests.core.compliance.review_test import _create_mock_record


def _create_mock_session(workspace):
    record = _create_mock_record(workspace)
    WorkflowStore(workspace).save(record)
    session = SimpleNamespace(
        workspace_path=workspace,
        inventory=SimpleNamespace(context_id="context"),
        bus=WebEventBus(),
        _closed=False,
    )
    request = {
        "type": "compliance.control",
        "context_id": "context",
        "request_id": "review-1",
        "run_id": record["id"],
        "profile": "fixture",
        "control_id": "fail",
    }
    return session, request


@pytest.mark.asyncio
async def test_control_and_export_requests_route_without_a_model_turn(tmp_path):
    session, request = _create_mock_session(tmp_path)
    consumer = ConsoleConsumer()
    consumer.session = session
    consumer.bus = session.bus
    await consumer.receive_json(request)
    event = session.bus.drain_nowait()[0]
    assert event["type"] == "compliance.control"
    assert event["payload"]["request_id"] == "review-1"
    assert (
        event["payload"]["control"]["before"]["tests"][0]["message"]
        == "raw-secret-failure"
    )
    await consumer.receive_json(
        {**request, "type": "compliance.export", "format": "json"}
    )
    events = session.bus.drain_nowait()
    assert [event["type"] for event in events] == ["compliance.export"]
    assert events[0]["payload"]["mime"] == "application/json"
    assert '"control_count": 6' in events[0]["payload"]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["context", "workspace", "closed"])
async def test_review_discards_response_when_engagement_retires(tmp_path, change):
    session, request = _create_mock_session(tmp_path)

    def export(workspace, record, format):
        result = export_report(workspace, record, format)
        if change == "context":
            session.inventory.context_id = "replacement"
        elif change == "workspace":
            session.workspace_path = tmp_path / "replacement"
        else:
            session._closed = True
        return result

    with patch("kerkov.web.runtime.compliance.export_report", side_effect=export):
        await review_saved_run(
            session, {**request, "type": "compliance.export", "format": "html"}
        )
    assert session.bus.drain_nowait() == []


@pytest.mark.asyncio
async def test_stale_context_is_rejected_before_reading_evidence(tmp_path):
    session, request = _create_mock_session(tmp_path)
    with patch("kerkov.web.runtime.compliance.WorkflowStore.load") as load:
        await review_saved_run(session, {**request, "context_id": "old-context"})
    load.assert_not_called()
    assert session.bus.drain_nowait() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"run_id": "../outside"},
        {"profile": {"bad": "value"}},
        {"type": "compliance.export", "format": "pdf"},
    ],
)
async def test_malformed_review_requests_return_correlated_errors(tmp_path, updates):
    session, request = _create_mock_session(tmp_path)
    await review_saved_run(session, {**request, **updates})
    payload = session.bus.drain_nowait()[0]["payload"]
    assert payload["request_id"] == request["request_id"]
    assert "Could not load" in payload["detail"]
    assert "body" not in payload


@pytest.mark.asyncio
async def test_interrupted_record_exports_unknown_final_outcome_without_rewriting(
    tmp_path,
):
    session, request = _create_mock_session(tmp_path)
    store = WorkflowStore(tmp_path)
    record = store.load(request["run_id"])
    record.update(status="running", detail="Assessing")
    store.save(record)
    await review_saved_run(
        session, {**request, "type": "compliance.export", "format": "json"}
    )
    payload = session.bus.drain_nowait()[0]["payload"]
    assert '"status": "incomplete"' in payload["body"]
    assert "No final workflow outcome was recorded" in payload["body"]
    assert store.load(request["run_id"])["status"] == "running"
