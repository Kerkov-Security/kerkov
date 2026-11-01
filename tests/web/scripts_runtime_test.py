"""Workspace `scripts/` listing, reading, traversal guarding and binary handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.scripts import MAX_FILES, MAX_READ_BYTES
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
    """Activate a workspace and return its `scripts/` directory."""
    workspace = tmp_path / "workspaces" / "engagement-1"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    web.workspace_path = workspace
    web.scripts.engagement_id = "engagement-1"
    return scripts


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
    scripts = _activate(session, tmp_path)
    (scripts / "exploit.py").write_text("print('hi')\n", encoding="utf-8")
    (scripts / "scan.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    nested = scripts / "poc"
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
async def test_list_excludes_files_outside_scripts_directory(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    scripts = _activate(session, tmp_path)
    workspace = scripts.parent
    (workspace / "approvals.yaml").write_text("rules: []\n", encoding="utf-8")
    (workspace / "grype-report.json").write_text("{}\n", encoding="utf-8")
    agent = scripts / "a1-recon"
    agent.mkdir()
    (agent / "enum.py").write_text("print('hi')\n", encoding="utf-8")

    events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert [row["path"] for row in payload["files"]] == ["a1-recon/enum.py"]


@pytest.mark.asyncio
async def test_list_without_scripts_directory_is_empty(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    scripts = _activate(session, tmp_path)
    scripts.rmdir()
    (scripts.parent / "notes.txt").write_text("x", encoding="utf-8")

    events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert payload["available"] is True
    assert payload["files"] == []
    assert payload["detail"] == "No scripts have been generated yet."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["outside", "workspace", "sibling"],
    ids=["outside-workspace", "workspace-root", "workspace-sibling"],
)
async def test_symlinked_scripts_directory_is_ignored(
    session: WebKerkovSession, tmp_path: Path, target: str
) -> None:
    scripts = _activate(session, tmp_path)
    workspace = scripts.parent
    scripts.rmdir()
    destination = {
        "outside": tmp_path / "host",
        "workspace": workspace,
        "sibling": workspace / "reports",
    }[target]
    destination.mkdir(exist_ok=True)
    (destination / "secret.py").write_text("secret", encoding="utf-8")
    try:
        scripts.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    events = await _request(session, "list")
    assert events[-1]["payload"]["files"] == []
    events = await _request(session, "read", path="secret.py")
    assert events[-1]["type"] == "scripts.error"


@pytest.mark.asyncio
async def test_list_truncation_boundary(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    scripts = _activate(session, tmp_path)
    for index in range(MAX_FILES):
        (scripts / f"f{index:05d}.txt").write_text("x", encoding="utf-8")

    events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert len(payload["files"]) == MAX_FILES
    assert payload["truncated"] is False

    (scripts / "f99999.txt").write_text("x", encoding="utf-8")
    events = await _request(session, "list")
    payload = events[-1]["payload"]
    assert len(payload["files"]) == MAX_FILES
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_read_returns_content_and_language(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    scripts = _activate(session, tmp_path)
    (scripts / "exploit.py").write_bytes(b"print('hi')\n")

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
    scripts = _activate(session, tmp_path)
    (scripts.parent / "secret.txt").write_text("secret", encoding="utf-8")

    events = await _request(session, "read", path="../secret.txt")
    assert events[-1]["type"] == "scripts.error"
    assert events[-1]["payload"]["operation"] == "read"


@pytest.mark.asyncio
async def test_read_detects_binary(session: WebKerkovSession, tmp_path: Path) -> None:
    scripts = _activate(session, tmp_path)
    (scripts / "blob.bin").write_bytes(b"\x00\x01\x02binary")

    events = await _request(session, "read", path="blob.bin")
    payload = events[-1]["payload"]
    assert payload["binary"] is True
    assert payload["content"] == ""


@pytest.mark.asyncio
async def test_read_truncates_large_files(
    session: WebKerkovSession, tmp_path: Path
) -> None:
    scripts = _activate(session, tmp_path)
    (scripts / "big.txt").write_text("a" * (MAX_READ_BYTES + 100), encoding="utf-8")

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
