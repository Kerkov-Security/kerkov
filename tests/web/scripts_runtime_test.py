"""Workspace scripts listing, reading, traversal guarding and binary handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.scripts import MAX_READ_BYTES
from kerkov.web.runtime.session import WebKerkovSession


@pytest.fixture
def session(tmp_path: Path):
    web = WebKerkovSession(
        WebEventBus(),
        config=ConfigManager(
            GOOGLE_API_KEY="test",
            KB_ENABLED=True,
            EXECUTION_ENABLED=False,
            WORKSPACE_DIR=str(tmp_path / "workspaces"),
            USER_APPROVALS_PATH=str(tmp_path / "approvals.yaml"),
            HTTP_PROFILES_PATH=str(tmp_path / "missing-http-profiles.yaml"),
        ),
    )
    yield web
    web.close()


def _activate(web: WebKerkovSession, tmp_path: Path) -> Path:
    workspace = tmp_path / "workspaces" / "engagement-1"
    workspace.mkdir(parents=True, exist_ok=True)
    web.workspace_path = workspace
    web.scripts.engagement_id = "engagement-1"
    return workspace


async def _request(web: WebKerkovSession, operation: str, **kwargs) -> list[dict]:
    web.bus.drain_nowait()
    content = {
        "type": f"scripts.{operation}",
        "request_id": "request-1",
        "context_id": web.scripts.context_id,
        **kwargs,
    }
    await web.handle_scripts_message(content)
    return web.bus.drain_nowait()


@pytest.mark.asyncio
async def test_list_without_workspace_is_unavailable(
    session: WebKerkovSession,
) -> None:
    events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert events[-1]["type"] == "scripts.snapshot"
    assert payload["available"] is False
    assert payload["files"] == []


@pytest.mark.asyncio
async def test_list_sorts_and_classifies_workspace_files(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    workspace = _activate(session, tmp_path)
    (workspace / "exploit.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "scan.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    nested = workspace / "poc"
    nested.mkdir()
    (nested / "payload.json").write_text('{"a": 1}\n', encoding="utf-8")

    events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert payload["available"] is True
    paths = [row["path"] for row in payload["files"]]
    assert paths == ["exploit.py", "poc/payload.json", "scan.sh"]
    languages = {row["path"]: row["language"] for row in payload["files"]}
    assert languages["exploit.py"] == "python"
    assert languages["scan.sh"] == "bash"
    assert languages["poc/payload.json"] == "json"


@pytest.mark.asyncio
async def test_read_returns_content_and_language(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    workspace = _activate(session, tmp_path)
    (workspace / "exploit.py").write_bytes(b"print('hi')\n")

    events = await _request(session, "read", path="exploit.py")
    payload = events[-1]["payload"]
    assert events[-1]["type"] == "scripts.content"
    assert payload["content"] == "print('hi')\n"
    assert payload["language"] == "python"
    assert payload["binary"] is False
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_read_rejects_path_traversal(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    _activate(session, tmp_path)
    secret = tmp_path / "workspaces" / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    events = await _request(session, "read", path="../secret.txt")
    assert events[-1]["type"] == "scripts.error"
    assert events[-1]["payload"]["operation"] == "read"


@pytest.mark.asyncio
async def test_read_detects_binary(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    workspace = _activate(session, tmp_path)
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    events = await _request(session, "read", path="blob.bin")
    payload = events[-1]["payload"]
    assert payload["binary"] is True
    assert payload["content"] == ""


@pytest.mark.asyncio
async def test_read_truncates_large_files(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    workspace = _activate(session, tmp_path)
    (workspace / "big.txt").write_text("a" * (MAX_READ_BYTES + 100), encoding="utf-8")

    events = await _request(session, "read", path="big.txt")
    payload = events[-1]["payload"]
    assert payload["truncated"] is True
    assert len(payload["content"]) == MAX_READ_BYTES


@pytest.mark.asyncio
async def test_read_with_stale_context_is_rejected(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    _activate(session, tmp_path)
    session.bus.drain_nowait()
    await session.handle_scripts_message(
        {
            "type": "scripts.read",
            "request_id": "request-1",
            "context_id": "stale",
            "path": "whatever",
        }
    )
    events = session.bus.drain_nowait()
    assert events[-1]["type"] == "scripts.error"


@pytest.mark.asyncio
async def test_reset_hides_files_and_resets_context(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    _activate(session, tmp_path)
    previous = session.scripts.context_id
    session.bus.drain_nowait()
    session.scripts.reset()
    events = session.bus.drain_nowait()
    assert events[-1]["type"] == "scripts.snapshot"
    assert events[-1]["payload"]["available"] is False
    assert session.scripts.context_id != previous
    assert session.scripts.engagement_id is None
