"""Compliance preconditions the browser shows before a run, and run identity."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kerkov.core.session.modes import ModeType
from kerkov.core.workflows.compliance import controller_blockers
from kerkov.core.workflows.registry import COMPLIANCE_WORKFLOW, mode_mismatch_notice
from kerkov.web.runtime.compliance import WebWorkflowObserver
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.web_runtime_test import _config, _FakeProvider


def _payload(events: list[dict], event_type: str) -> dict:
    return next(event["payload"] for event in events if event["type"] == event_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_enabled", [True, False])
async def test_ready_and_mode_switch_carry_the_server_preconditions(
    tmp_path: Path, execution_enabled: bool
) -> None:
    bus = WebEventBus()
    config = _config(tmp_path, execution_enabled=execution_enabled)
    session = WebKerkovSession(bus, config=config, provider=_FakeProvider([]))
    await session.initialize()
    try:
        ready = _payload(bus.drain_nowait(), "session.ready")["compliance"]
        assert ready["blockers"] == controller_blockers(config)
        assert any("EXECUTION_ENABLED" in b for b in ready["blockers"]) is (
            not execution_enabled
        )
        assert ready["mode_notice"] == mode_mismatch_notice(
            COMPLIANCE_WORKFLOW, ModeType.PURPLE
        )

        await session.handle_user_message("/mode blue")
        changed = _payload(bus.drain_nowait(), "mode.changed")["compliance"]
        assert changed["blockers"] == ready["blockers"]
        assert changed["mode_notice"] is None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_windows_controller_is_reported_before_any_run(tmp_path: Path) -> None:
    bus = WebEventBus()
    session = WebKerkovSession(
        bus,
        config=_config(tmp_path, execution_enabled=True),
        provider=_FakeProvider([]),
    )
    with patch("kerkov.core.workflows.compliance.sys.platform", "win32"):
        await session.initialize()
    try:
        ready = _payload(bus.drain_nowait(), "session.ready")["compliance"]
        assert ready["blockers"] == [
            "Use Linux, macOS or WSL as the compliance controller"
        ]
    finally:
        session.close()


def test_run_updates_name_the_workflow_message_that_started_them() -> None:
    bus = WebEventBus()
    WebWorkflowObserver(bus, "context-1", "message-1").updated({"id": "run"})
    payload = _payload(bus.drain_nowait(), "compliance.updated")
    assert payload == {
        "context_id": "context-1",
        "message_id": "message-1",
        "run": {"id": "run"},
    }
