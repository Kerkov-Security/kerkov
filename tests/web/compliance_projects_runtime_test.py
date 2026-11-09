"""Read-only source requests are bounded, cancellable and engagement-scoped."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kerkov.core.compliance.projects import ProfileProjects
from kerkov.tools.compliance_profile_tool import ComplianceProfileTool
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.projects import read_projects
from tests.core.compliance.projects_test import (
    COMMIT,
    URL,
    _create_mock_github,
    _FakePreparationRuntime,
)
from tests.web.inventory_transport_test import _communicator, runtime


async def _until(communicator, event_type):
    while True:
        event = await communicator.receive_json_from(timeout=5)
        if event["type"] == event_type:
            return event["payload"]


def _create_mock_session(tmp_path):
    return SimpleNamespace(
        inventory=SimpleNamespace(context_id="context"),
        config=SimpleNamespace(
            compliance_projects_dir=str(tmp_path), cinc_docker_image="fixture/cinc:7"
        ),
        bus=WebEventBus(),
        _closed=False,
    )


@pytest.mark.asyncio
async def test_validation_is_read_only_and_stale_responses_are_discarded(tmp_path):
    session = _create_mock_session(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def validate(url):
        entered.set()
        await release.wait()
        return {"url": url, "name": "example"}

    projects = SimpleNamespace(
        remote=SimpleNamespace(validate=validate),
        get=Mock(),
        list=Mock(return_value=[]),
    )
    with patch("kerkov.web.runtime.projects.ProfileProjects", return_value=projects):
        task = asyncio.create_task(
            read_projects(
                session,
                {
                    "type": "compliance.project.validate",
                    "context_id": "context",
                    "request_id": "request",
                    "url": "https://github.com/example/project",
                },
            )
        )
        await entered.wait()
        session.inventory.context_id = "next"
        release.set()
        await task
    assert session.bus.drain_nowait() == []
    projects.get.assert_not_called()


@pytest.mark.asyncio
async def test_url_errors_do_not_start_network_or_preparation(tmp_path):
    session = _create_mock_session(tmp_path)
    remote = AsyncMock()
    with patch(
        "kerkov.web.runtime.projects.ProfileProjects",
        return_value=SimpleNamespace(remote=remote),
    ):
        await read_projects(
            session,
            {
                "type": "compliance.project.validate",
                "context_id": "context",
                "request_id": "request",
                "url": "http://127.0.0.1/secret",
            },
        )
    remote.validate.assert_not_called()
    event = await session.bus.next_event()
    assert "detail" in event["payload"]
    assert event["payload"]["request_id"] == "request"


@pytest.mark.asyncio
async def test_slow_project_requests_keep_websocket_responsive_and_cancel_on_disconnect(
    runtime,
):
    communicator = _communicator()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(session, content):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch("kerkov.web.console.consumers.read_projects", side_effect=slow):
        assert (await communicator.connect())[0]
        await _until(communicator, "session.ready")
        web = runtime[0]
        request = {
            "type": "compliance.project.validate",
            "context_id": web.inventory.context_id,
            "request_id": "one",
            "url": "https://github.com/example/project",
        }
        await communicator.send_json_to(request)
        await asyncio.wait_for(entered.wait(), 5)
        await communicator.send_json_to({**request, "request_id": "two"})
        await communicator.send_json_to({**request, "request_id": "three"})
        payload = await _until(communicator, "compliance.project.validate")
        assert payload["request_id"] == "three"
        assert "still running" in payload["detail"]
        await communicator.disconnect()
        assert cancelled.is_set()


def _configure_preparation(web, projects):
    web.config = web.config.model_copy(update={"execution_enabled": True})
    web.set_approval_mode("strict")
    tool = next(tool for tool in web.tools if isinstance(tool, ComplianceProfileTool))
    tool.config = web.config
    tool.projects = projects


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Profile preparation requires POSIX")
async def test_prepare_needs_no_provider_and_rejects_an_unreviewed_revision(
    runtime, tmp_path
):
    remote, calls = _create_mock_github(revision=lambda: "b" * 40)
    projects = ProfileProjects(
        tmp_path / "cache", remote=remote, runtime=_FakePreparationRuntime()
    )
    communicator = _communicator()
    assert (await communicator.connect())[0]
    try:
        await _until(communicator, "session.ready")
        web = runtime[0]
        assert web.provider is None
        _configure_preparation(web, projects)
        await communicator.send_json_to(
            {
                "type": "compliance.project.prepare",
                "context_id": web.inventory.context_id,
                "message_id": "prepare",
                "project_request": {"url": URL, "commit": COMMIT},
            }
        )
        approval = await _until(communicator, "approval.request")
        assert approval["params"] == {
            "action": "prepare",
            "url": URL,
            "refresh": False,
            "expected_commit": COMMIT,
        }
        await communicator.send_json_to(
            {
                "type": "approval.response",
                "approval_id": approval["approval_id"],
                "decision": "approve_once",
            }
        )
        await _until(communicator, "user.message.complete")
        assert projects.get(URL) is None
        assert not any("/tarball/" in call for call in calls)
    finally:
        await communicator.disconnect()
