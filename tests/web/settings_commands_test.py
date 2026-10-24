"""The web console's /verbosity, /approval, /autonomy settings commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession, web_command


def _config(tmp_path: Path, **overrides) -> ConfigManager:
    return ConfigManager(
        **overrides,
        GOOGLE_API_KEY="test",
        EXECUTION_ENABLED=False,
        WORKSPACE_DIR=str(tmp_path / "workspaces"),
        USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
        HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
    )


def _session(tmp_path: Path, **overrides) -> WebKerkovSession:
    return WebKerkovSession(WebEventBus(), config=_config(tmp_path, **overrides))


def _last(bus: WebEventBus, event_type: str) -> dict:
    events = [e for e in bus.drain_nowait() if e["type"] == event_type]
    assert events, f"no {event_type} event emitted"
    return events[-1]["payload"]


@pytest.mark.parametrize(
    "command",
    [
        "/verbosity",
        "/verbosity silent",
        "/approval",
        "/approval strict",
        "/autonomy",
        "/autonomy aggressive",
    ],
)
def test_settings_commands_are_local_not_stoppable(command: str) -> None:
    # Non-None classification keeps them off the stoppable engine path.
    assert web_command(command) is not None


@pytest.mark.asyncio
async def test_verbosity_change_updates_manager_and_emits(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await session.handle_user_message("/verbosity silent")
    assert session.verbosity_manager.current.value == "silent"
    payload = _last(session.bus, "settings.changed")
    assert payload["setting"] == "verbosity"
    assert "silent" in payload["title"]


@pytest.mark.asyncio
async def test_approval_change_updates_manager_and_carries_mode(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await session.handle_user_message("/approval strict")
    assert session.approval_mode_manager.current.value == "strict"
    payload = _last(session.bus, "settings.changed")
    assert payload["setting"] == "approval"
    assert payload["approval_mode"] == "strict"


@pytest.mark.asyncio
async def test_autonomy_change_sets_both_axes(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await session.handle_user_message("/autonomy aggressive")
    # Autonomy drives verbosity + approval together.
    assert session.approval_mode_manager.current.value
    payload = _last(session.bus, "settings.changed")
    assert payload["setting"] == "autonomy"
    assert "aggressive" in payload["title"]
    assert payload["approval_mode"] == session.approval_mode_manager.current.value


@pytest.mark.asyncio
async def test_bare_settings_command_reports_current(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await session.handle_user_message("/verbosity")
    payload = _last(session.bus, "settings.info")
    assert payload["setting"] == "verbosity"


@pytest.mark.asyncio
async def test_invalid_value_emits_error_and_leaves_setting(tmp_path: Path) -> None:
    session = _session(tmp_path)
    before = session.verbosity_manager.current
    await session.handle_user_message("/verbosity bogus")
    assert session.verbosity_manager.current is before
    assert _last(session.bus, "error")["detail"]


def test_set_approval_mode_refuses_yolo_without_allow_autonomous(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    session.set_approval_mode("yolo")
    assert session.approval_mode_manager.current.value == "normal"
    assert "ALLOW_AUTONOMOUS" in _last(session.bus, "error")["detail"]


def test_set_approval_mode_allows_yolo_when_autonomous(tmp_path: Path) -> None:
    session = _session(tmp_path, ALLOW_AUTONOMOUS=True)
    session.set_approval_mode("yolo")
    assert session.approval_mode_manager.current.value == "yolo"
    payload = _last(session.bus, "settings.changed")
    assert payload["approval_mode"] == "yolo"


def test_set_approval_mode_rejects_unknown_value(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.set_approval_mode("reckless")
    assert session.approval_mode_manager.current.value == "normal"
    assert _last(session.bus, "error")["detail"].startswith("Invalid approval mode")
