"""Read-only source requests are bounded, cancellable and engagement-scoped."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kerkov.core.compliance.projects import ProfileProjects
from kerkov.tools.compliance_profile_tool import ComplianceProfileTool
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.projects import read_projects
from tests.core.compliance.openscap_projects_test import (
    DATASTREAM,
    _create_mock_scap_github,
)
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
@pytest.mark.parametrize(
    "event", ["compliance.project.validate", "compliance.project.cached"]
)
async def test_datastream_selector_reaches_read_only_project_requests(tmp_path, event):
    session = _create_mock_session(tmp_path)
    projects = SimpleNamespace(
        remote=SimpleNamespace(validate=AsyncMock(return_value={"engine": "openscap"})),
        get=Mock(return_value={"engine": "openscap"}),
    )
    with patch("kerkov.web.runtime.projects.ProfileProjects", return_value=projects):
        await read_projects(
            session,
            {
                "type": event,
                "context_id": "context",
                "request_id": "scap",
                "url": URL,
                "datastream": DATASTREAM,
            },
        )
    if event.endswith("validate"):
        projects.remote.validate.assert_awaited_once_with(URL, datastream=DATASTREAM)
    else:
        projects.get.assert_called_once_with(URL, verify=True, datastream=DATASTREAM)
    assert session.bus.drain_nowait()[0]["payload"]["request_id"] == "scap"


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
async def test_scap_preparation_approves_exact_path_and_commit_without_cinc(
    runtime, tmp_path
):
    remote, _, calls = _create_mock_scap_github()
    scanner = AsyncMock()
    projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=scanner)
    communicator = _communicator()
    assert (await communicator.connect())[0]
    try:
        await _until(communicator, "session.ready")
        web = runtime[0]
        _configure_preparation(web, projects)
        await communicator.send_json_to(
            {
                "type": "compliance.project.prepare",
                "context_id": web.inventory.context_id,
                "message_id": "prepare-scap",
                "project_request": {
                    "url": URL,
                    "commit": COMMIT,
                    "datastream": DATASTREAM,
                },
            }
        )
        approval = await _until(communicator, "approval.request")
        assert approval["params"]["datastream"] == DATASTREAM
        assert approval["params"]["expected_commit"] == COMMIT
        assert calls == []
        await communicator.send_json_to(
            {
                "type": "approval.response",
                "approval_id": approval["approval_id"],
                "decision": "approve_once",
            }
        )
        await _until(communicator, "user.message.complete")
        assert (
            projects.get(URL, datastream=DATASTREAM, verify=True)["engine"]
            == "openscap"
        )
        scanner.run.assert_not_called()
    finally:
        await communicator.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Profile preparation requires POSIX")
async def test_prepare_needs_no_provider_and_rejects_an_unreviewed_revision(
    runtime, tmp_path
):
    remote, calls = _create_mock_github(
        revision=lambda: "b" * 40, comparison="diverged"
    )
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


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Profile preparation requires POSIX")
@pytest.mark.parametrize("outcome", ["approve", "deny", "stop"])
async def test_refresh_uses_the_approved_commit_or_preserves_the_ready_copy(
    runtime, tmp_path, outcome
):
    branch_commit = COMMIT
    remote, calls = _create_mock_github(
        revision=lambda: branch_commit, comparison="behind"
    )
    scanner = _FakePreparationRuntime()
    projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=scanner)
    previous = await projects.prepare(URL, tmp_path / "initial", expected_commit=COMMIT)
    calls.clear()
    scanner.calls.clear()

    approved_commit = branch_commit = "b" * 40
    communicator = _communicator()
    assert (await communicator.connect())[0]
    try:
        await _until(communicator, "session.ready")
        web = runtime[0]
        assert web.provider is None
        _configure_preparation(web, projects)
        request = {
            "type": "compliance.project.prepare",
            "context_id": web.inventory.context_id,
            "message_id": "refresh",
            "project_request": {"url": URL, "refresh": True, "commit": approved_commit},
        }
        await communicator.send_json_to(request)
        assert (await _until(communicator, "user.message.accepted"))["stoppable"]
        approval = await _until(communicator, "approval.request")
        assert approval["params"] == {
            "action": "prepare",
            "url": URL,
            "refresh": True,
            "expected_commit": approved_commit,
        }
        assert URL in approval["call"] and approved_commit in approval["call"]
        assert not any("/tarball/" in call for call in calls)
        branch_commit = "c" * 40
        if outcome == "stop":
            scanner.started, scanner.release = asyncio.Event(), asyncio.Event()
        await communicator.send_json_to(
            {
                "type": "approval.response",
                "approval_id": approval["approval_id"],
                "decision": "deny" if outcome == "deny" else "approve_once",
            }
        )
        if outcome == "stop":
            await asyncio.wait_for(scanner.started.wait(), 5)
            await communicator.send_json_to(
                {"type": "user.message.stop", "message_id": "refresh"}
            )
        complete = await _until(communicator, "user.message.complete")
        assert complete == {"message_id": "refresh", "stopped": outcome == "stop"}

        ready = projects.get(URL, verify=True)
        if outcome == "approve":
            assert ready["commit"] == approved_commit
            assert ready["generation"] != previous["generation"]
            assert not Path(previous["profile"]).exists()
            assert ready["ref"] == "main"
        else:
            assert ready["generation"] == previous["generation"]
            assert ready["commit"] == COMMIT
        downloads = [call for call in calls if "/tarball/" in call]
        if outcome == "deny":
            assert downloads == scanner.calls == []
        else:
            assert downloads == [
                f"https://api.github.com/repos/example/profile/tarball/{approved_commit}",
                f"https://codeload.github.com/repos/example/profile/tarball/{approved_commit}",
            ]
            assert len(scanner.calls) == 1
        assert sum("/commits/" in call for call in calls) == (outcome != "deny")
    finally:
        await communicator.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Profile preparation requires POSIX")
@pytest.mark.parametrize("comparison", ["behind", "diverged"])
async def test_prepare_only_downloads_a_commit_reachable_from_the_source_ref(
    runtime, tmp_path, comparison
):
    remote, calls = _create_mock_github(
        revision=lambda: "b" * 40, comparison=comparison
    )
    projects = ProfileProjects(
        tmp_path / "cache", remote=remote, runtime=_FakePreparationRuntime()
    )
    communicator = _communicator()
    assert (await communicator.connect())[0]
    try:
        await _until(communicator, "session.ready")
        web = runtime[0]
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
        assert approval["params"]["expected_commit"] == COMMIT
        await communicator.send_json_to(
            {
                "type": "approval.response",
                "approval_id": approval["approval_id"],
                "decision": "approve_once",
            }
        )
        await _until(communicator, "user.message.complete")
        ready = projects.get(URL, verify=True)
        if comparison == "behind":
            assert ready["commit"] == COMMIT
            assert ready["ref"] == "main"
        else:
            assert ready is None
            assert not any(
                "/contents/" in call or "/tarball/" in call for call in calls
            )
        assert any(f"/compare/{'b' * 40}...{COMMIT}" in call for call in calls)
    finally:
        await communicator.disconnect()
