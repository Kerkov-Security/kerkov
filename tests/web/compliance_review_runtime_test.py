"""Local control and export requests stay bound to their saved engagement."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kerkov.core.compliance.review import ReviewEvidence, export_report
from kerkov.core.compliance.workflows import WorkflowStore
from kerkov.web.console.consumers import ConsoleConsumer
from kerkov.web.runtime.compliance import read_compliance_details
from kerkov.web.runtime.events import WebEventBus
from tests.core.compliance.openscap_test import DATASTREAM_FIXTURE
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
    consumer._compliance_review_task = None
    consumer._review_evidence = ReviewEvidence()
    await consumer.receive_json(request)
    await consumer._compliance_review_task
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
    await consumer._compliance_review_task
    events = session.bus.drain_nowait()
    assert [event["type"] for event in events] == ["compliance.export"]
    assert events[0]["payload"]["mime"] == "application/json"
    assert '"control_count": 6' in events[0]["payload"]["body"]


@pytest.mark.asyncio
async def test_datastream_inspection_routes_through_local_review(tmp_path):
    session, request = _create_mock_session(tmp_path)
    session.config = SimpleNamespace(execution_enabled=False)
    datastream = tmp_path / "content.xml"
    datastream.write_bytes(DATASTREAM_FIXTURE.read_bytes())
    consumer = ConsoleConsumer()
    consumer.session = session
    consumer.bus = session.bus
    consumer._compliance_review_task = None
    consumer._review_evidence = ReviewEvidence()
    with patch("kerkov.web.runtime.compliance.ansible_binary", return_value=None):
        await consumer.receive_json(
            {
                "type": "compliance.datastream.inspect",
                "context_id": request["context_id"],
                "request_id": "inspect-1",
                "path": "content.xml",
            }
        )
        await consumer._compliance_review_task
    event = session.bus.drain_nowait()[0]
    assert event["type"] == "compliance.datastream.inspect"
    assert event["payload"]["request_id"] == "inspect-1"
    assert (
        event["payload"]["sha256"]
        == hashlib.sha256(datastream.read_bytes()).hexdigest()
    )
    assert event["payload"]["profiles"][0] == {
        "id": "xccdf_example_profile_server",
        "title": "Server baseline",
    }
    assert event["payload"]["ansible_available"] is False


@pytest.mark.asyncio
async def test_datastream_inspection_rejects_relative_workspace_escape(tmp_path):
    session, request = _create_mock_session(tmp_path)
    await read_compliance_details(
        session,
        {**request, "type": "compliance.datastream.inspect", "path": "../content.xml"},
    )
    payload = session.bus.drain_nowait()[0]["payload"]
    assert (
        "relative paths must stay inside the engagement workspace" in payload["detail"]
    )
    assert "sha256" not in payload and "profiles" not in payload


@pytest.mark.asyncio
async def test_ansible_can_be_rechecked_without_reading_a_datastream(tmp_path):
    session, request = _create_mock_session(tmp_path)
    consumer = ConsoleConsumer()
    consumer.session = session
    consumer.bus = session.bus
    consumer._compliance_review_task = None
    consumer._review_evidence = ReviewEvidence()
    with (
        patch(
            "kerkov.web.runtime.compliance.ansible_binary",
            return_value="/bin/ansible-playbook",
        ),
        patch("kerkov.web.runtime.compliance.local_file") as read_file,
    ):
        await consumer.receive_json({**request, "type": "compliance.ansible.check"})
        await consumer._compliance_review_task
    read_file.assert_not_called()
    event = session.bus.drain_nowait()[0]
    assert event["type"] == "compliance.ansible.check"
    assert event["payload"]["request_id"] == request["request_id"]
    assert event["payload"]["ansible_available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["context", "workspace", "closed"])
async def test_review_discards_response_when_engagement_retires(tmp_path, change):
    session, request = _create_mock_session(tmp_path)

    def export(workspace, record, format, *, evidence):
        result = export_report(workspace, record, format, evidence=evidence)
        if change == "context":
            session.inventory.context_id = "replacement"
        elif change == "workspace":
            session.workspace_path = tmp_path / "replacement"
        else:
            session._closed = True
        return result

    with patch("kerkov.web.runtime.compliance.export_report", side_effect=export):
        await read_compliance_details(
            session, {**request, "type": "compliance.export", "format": "html"}
        )
    assert session.bus.drain_nowait() == []


@pytest.mark.asyncio
async def test_stale_context_is_rejected_before_reading_evidence(tmp_path):
    session, request = _create_mock_session(tmp_path)
    with patch("kerkov.web.runtime.compliance.WorkflowStore.load") as load:
        await read_compliance_details(session, {**request, "context_id": "old-context"})
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
    await read_compliance_details(session, {**request, **updates})
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
    await read_compliance_details(
        session, {**request, "type": "compliance.export", "format": "json"}
    )
    payload = session.bus.drain_nowait()[0]["payload"]
    assert '"status": "incomplete"' in payload["body"]
    assert "No final workflow outcome was recorded" in payload["body"]
    assert store.load(request["run_id"])["status"] == "running"


@pytest.mark.asyncio
async def test_review_requests_leave_stop_responsive_and_reject_overlap(tmp_path):
    session, request = _create_mock_session(tmp_path)
    consumer = ConsoleConsumer()
    consumer.session = session
    consumer.bus = session.bus
    consumer._compliance_review_task = None
    consumer._review_evidence = ReviewEvidence()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def review(*args):
        entered.set()
        await release.wait()

    with (
        patch(
            "kerkov.web.console.consumers.read_compliance_details", side_effect=review
        ),
        patch.object(consumer, "_stop_message") as stop,
    ):
        await consumer.receive_json(request)
        task = consumer._compliance_review_task
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await consumer.receive_json({**request, "request_id": "overlap"})
            payload = session.bus.drain_nowait()[0]["payload"]
            assert payload["request_id"] == "overlap"
            assert "still running" in payload["detail"]
            await consumer.receive_json(
                {"type": "user.message.stop", "message_id": "turn"}
            )
            stop.assert_called_once()
        finally:
            release.set()
            await task


@pytest.mark.asyncio
async def test_control_page_routes_through_read_only_review(tmp_path):
    session, request = _create_mock_session(tmp_path)
    consumer = ConsoleConsumer()
    consumer.session = session
    consumer.bus = session.bus
    consumer._compliance_review_task = None
    consumer._review_evidence = ReviewEvidence()
    await consumer.receive_json(
        {
            **request,
            "type": "compliance.controls",
            "query": {"search": "skip", "page_size": 25},
        }
    )
    await consumer._compliance_review_task
    event = session.bus.drain_nowait()[0]
    assert event["type"] == "compliance.controls"
    assert event["payload"]["request_id"] == request["request_id"]
    assert event["payload"]["total"] == 6
    assert event["payload"]["matched"] == 1
    assert event["payload"]["controls"][0]["control_id"] == "skip"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        {"page": 0},
        {"page": True},
        {"page_size": 10000},
        {"search": "x" * 257},
        {"sort": "unexpected"},
        {"impact": "critical"},
    ],
)
async def test_control_page_rejects_invalid_queries(tmp_path, query):
    session, request = _create_mock_session(tmp_path)
    await read_compliance_details(
        session, {**request, "type": "compliance.controls", "query": query}
    )
    payload = session.bus.drain_nowait()[0]["payload"]
    assert payload["request_id"] == request["request_id"]
    assert "detail" in payload
    assert "controls" not in payload
