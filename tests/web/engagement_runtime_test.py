"""Saved engagements restore state without corrupting the current session."""

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kerkov.cli.engagement_store import EngagementStore
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.models.basic_models import LLMResponse, Message
from kerkov.web.runtime import engagements as engagement_runtime
from kerkov.web.runtime.events import WebEventBus
from kerkov.web.runtime.session import WebKerkovSession
from tests.web.web_runtime_test import _config, _FakeProvider


@pytest.fixture
def session(tmp_path):
    instance = WebKerkovSession(
        WebEventBus(), config=_config(tmp_path), provider=_FakeProvider([])
    )
    yield instance
    instance.close()


def saved(session, *, kind="conversation", approval="normal", messages=None):
    store = EngagementStore(session.engagement_store.store_dir)
    ident = store.start_engagement(
        mode="Recon",
        model="old-model",
        verbosity="guided",
        approval_mode=approval,
        kind=kind,
    )
    for message in messages or [
        Message(role="system", content="saved context"),
        Message(role="user", content="Inspect localhost"),
        Message(role="assistant", content="Saved finding"),
    ]:
        store.append_message(message)
    return store, ident


async def request(session, operation, **fields):
    await session.handle_engagement_message(
        {
            "type": f"engagement.{operation}",
            "request_id": "test",
            "context_id": session.engagements.context_id,
            **fields,
        }
    )
    events = []
    while not session.bus._queue.empty():
        events.append(await session.bus.next_event())
    return next(
        e["payload"] for e in reversed(events) if e["type"].startswith("engagement.")
    )


@pytest.mark.asyncio
async def test_resume_keeps_provider_raw_history_workspace_and_ledger(session):
    await session.initialize()
    provider = session.provider
    store, ident = saved(
        session,
        messages=[
            Message(role="system", content="context"),
            Message(
                role="assistant",
                content="tool call",
                raw_blocks=[
                    {"type": "tool_use", "id": "call-1", "name": "http", "input": {}}
                ],
            ),
            Message(role="tool", content="result", call_id="call-1"),
            Message(role="assistant", content="interrupted", partial=True),
        ],
    )
    original = (store.store_dir / f"{ident}.jsonl").read_bytes()
    result = await request(session, "resume", engagement_id=ident)
    assert result["engagement_id"] == ident
    assert result["mode"] == "recon"
    assert session.provider is provider
    assert session.conversation_history[1].raw_blocks[0]["id"] == "call-1"
    assert session.conversation_history[2].call_id == "call-1"
    assert session.conversation_history[-1].partial
    assert session.workspace_path.name == ident
    assert session.inventory.engagement_id == ident
    assert session.findings_ledger is not None
    await session.findings_ledger.save_resource(
        LedgerSection.ASSETS, {"kind": "domain", "value": "example.com"}
    )
    await request(session, "new")
    assert session.engagement_store.engagement_id is None
    assert session.inventory.engagement_id is None
    assert not session.conversation_history
    await request(session, "resume", engagement_id=ident)
    rows = await session.findings_ledger.list(sections=(LedgerSection.ASSETS,))
    assert rows[0].entry["value"] == "example.com"
    assert (store.store_dir / f"{ident}.jsonl").read_bytes() == original


@pytest.mark.asyncio
async def test_next_turn_appends_to_resumed_root_and_agents_use_loaded_store(session):
    await session.initialize()
    store, ident = saved(session)
    await request(session, "resume", engagement_id=ident)
    session.provider.responses = [LLMResponse(content="continued", model="fake-model")]
    await session.handle_user_message("continue here")
    loaded = store.load_engagement(ident)
    assert loaded.messages[-2].content == "continue here"
    assert loaded.messages[-1].content == "continued"
    assert session.agent_supervisor._engagement_store is session.engagement_store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["../outside", "/absolute", "missing", "", "a/b", "x\\y"]
)
async def test_invalid_id_preserves_current_state(session, invalid):
    await session.initialize()
    session._apply_system_prompt_if_needed()
    original_id = session.engagement_store.engagement_id
    original_history = session.conversation_history
    result = await request(session, "resume", engagement_id=invalid)
    assert "detail" in result
    assert session.engagement_store.engagement_id == original_id
    assert session.conversation_history is original_history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["agent", "corrupt", "mode", "yolo", "mismatch", "symlink"]
)
async def test_unrestorable_engagement_does_not_retire_current(session, case):
    await session.initialize()
    session._apply_system_prompt_if_needed()
    current = session.engagement_store.engagement_id
    store, ident = saved(
        session,
        kind="agent" if case == "agent" else "conversation",
        approval="yolo" if case == "yolo" else "normal",
    )
    path = store.store_dir / f"{ident}.jsonl"
    if case == "corrupt":
        path.write_text("{broken")
    elif case in {"mode", "mismatch"}:
        lines = path.read_text().splitlines()
        meta = json.loads(lines[0])
        meta["mode" if case == "mode" else "id"] = "unknown"
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines))
    elif case == "symlink":
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    result = await request(session, "resume", engagement_id=ident)
    assert "detail" in result
    assert session.engagement_store.engagement_id == current
    assert session.inventory.engagement_id == current


@pytest.mark.asyncio
async def test_list_searches_all_conversations_before_paging_and_skips_bad_files(
    session,
):
    for i in range(23):
        store, _ = saved(session)
        store.update_summary(f"Assessment {i}")
    saved(session, kind="agent")
    (session.engagement_store.store_dir / "bad.jsonl").write_text("{broken")
    page = await request(session, "list")
    assert len(page["engagements"]) == 20
    assert page["next_offset"] == 20
    page2 = await request(session, "list", offset=20)
    assert len(page2["engagements"]) == 3
    assert page2["next_offset"] is None
    result = await request(session, "list", query="ASSESSMENT 22")
    assert len(result["engagements"]) == 1
    assert result["engagements"][0]["summary"] == "Assessment 22"


@pytest.mark.asyncio
async def test_list_uses_first_user_message_as_title_without_creating_engagement(
    session,
):
    saved(session)
    result = await request(session, "list", query="localhost")
    assert result["engagements"][0]["summary"] == "Inspect localhost"
    assert session.engagement_store.engagement_id is None


@pytest.mark.asyncio
async def test_history_is_bounded_paginated_and_excludes_system_and_raw_blocks(session):
    _, ident = saved(
        session,
        messages=[
            Message(role="system", content="private system"),
            *[Message(role="user", content=str(i) + "x" * 9000) for i in range(75)],
        ],
    )
    result = await request(session, "resume", engagement_id=ident)
    seen = []
    while True:
        assert sum(len(m["content"]) for m in result["messages"]) <= 64000
        assert all(m["truncated"] and "raw_blocks" not in m for m in result["messages"])
        seen = [m["content"].split("x")[0] for m in result["messages"]] + seen
        if result["before"] is None:
            break
        result = await request(session, "history", before=result["before"])
    assert seen == list(map(str, range(75)))


@pytest.mark.asyncio
async def test_history_renders_assistant_markdown_within_the_page_budget(session):
    _, ident = saved(
        session,
        messages=[
            Message(role="system", content="context"),
            Message(role="user", content="**not rendered**"),
            Message(
                role="assistant",
                content="Checking",
                raw_blocks=[
                    {"type": "tool_use", "id": "call-1", "name": "http", "input": {}}
                ],
            ),
            Message(role="tool", content="<b>raw</b>", call_id="call-1"),
            *[Message(role="assistant", content="<x>" * 2000) for _ in range(20)],
            Message(role="assistant", content="## Finding\n\n<script>x</script>"),
        ],
    )
    result = await request(session, "resume", engagement_id=ident)
    newest = result["messages"][-1]
    assert newest["html"] == "<h2>Finding</h2>\n<p>&lt;script&gt;x&lt;/script&gt;</p>\n"
    assert result["before"] is not None
    assert sum(len(m["content"]) + len(m["html"]) for m in result["messages"]) <= (
        engagement_runtime.HISTORY_PAGE_CHARS
    )
    while result["before"] is not None:
        result = await request(session, "history", before=result["before"])
    user, narration, tool = result["messages"][:3]
    assert "html" not in user and "html" not in tool
    assert narration["html"] == "<p>Checking</p>\n"


@pytest.mark.asyncio
async def test_busy_stale_and_another_tab_are_rejected(session, tmp_path):
    _, ident = saved(session)
    async with session._lock:
        assert (
            "Wait" in (await request(session, "resume", engagement_id=ident))["detail"]
        )
    assert (
        "changed"
        in (await request(session, "resume", engagement_id=ident, context_id="stale"))[
            "detail"
        ]
    )
    session.agent_supervisor.active = lambda: [object()]
    assert "Wait" in (await request(session, "resume", engagement_id=ident))["detail"]
    session.agent_supervisor.active = lambda: []
    await request(session, "resume", engagement_id=ident)
    other = WebKerkovSession(
        WebEventBus(), config=_config(tmp_path), provider=_FakeProvider([])
    )
    try:
        assert (
            "another browser"
            in (await request(other, "resume", engagement_id=ident))["detail"]
        )
        session.close()
        assert (await request(other, "resume", engagement_id=ident))[
            "engagement_id"
        ] == ident
    finally:
        other.close()


@pytest.mark.asyncio
async def test_bad_workspace_rules_leave_current_conversation_untouched(session):
    await session.initialize()
    session._apply_system_prompt_if_needed()
    current = session.engagement_store.engagement_id
    _, ident = saved(session)
    workspace = session.workspace_manager.path_for_engagement(ident)
    workspace.mkdir(parents=True)
    (workspace / "approvals.yaml").write_text("rules: [broken")
    result = await request(session, "resume", engagement_id=ident)
    assert "invalid workspace approvals.yaml" in result["detail"]
    assert session.engagement_store.engagement_id == current
    assert session.inventory.engagement_id == current
    assert session.knowledge_base is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["history", "raw_blocks", "output", "ledger"])
async def test_resume_rejects_oversized_candidate_before_retiring_current(
    session, monkeypatch, oversized
):
    await session.initialize()
    session._apply_system_prompt_if_needed()
    current_id = session.engagement_store.engagement_id
    current_history = session.conversation_history
    current_kb = session.knowledge_base
    current_context = session.engagements.context_id
    session.token_tracker.record_usage(1234, 56)
    session.tools = []
    monkeypatch.setattr(
        session.provider, "get_max_context_tokens", AsyncMock(return_value=16_000)
    )
    monkeypatch.setattr(
        session.provider,
        "get_max_output_tokens",
        AsyncMock(return_value=15_000 if oversized == "output" else 1000),
    )
    content = "x" * (60_000 if oversized == "history" else 4000)
    store, ident = saved(
        session,
        messages=[
            Message(role="system", content="context"),
            Message(
                role="assistant",
                content=content,
                raw_blocks=(
                    [{"type": "text", "text": "x" * 60_000}]
                    if oversized == "raw_blocks"
                    else None
                ),
            ),
        ],
    )
    path = store.store_dir / f"{ident}.jsonl"
    original = path.read_bytes()
    engagement_runtime.kb_path_for(store.store_dir, ident).touch()
    candidate_kb = Mock()
    opened = Mock(return_value=candidate_kb)
    monkeypatch.setattr(engagement_runtime, "open_knowledge_base", opened)
    monkeypatch.setattr(
        engagement_runtime,
        "FindingsLedger",
        Mock(
            return_value=Mock(
                serialize=AsyncMock(
                    return_value="x" * 60_000 if oversized == "ledger" else ""
                )
            )
        ),
    )

    result = await request(session, "resume", engagement_id=ident)

    assert "too large" in result["detail"]
    assert "larger-context model" in result["detail"]
    assert session.engagement_store.engagement_id == current_id
    assert session.inventory.engagement_id == current_id
    assert session.conversation_history is current_history
    assert session.knowledge_base is current_kb
    assert session.engagements.context_id == current_context
    assert session.token_tracker.total_tokens == 1290
    assert session.token_tracker.cumulative_tokens == 1290
    assert session.provider.requests == []
    assert path.read_bytes() == original
    assert session.engagements._key(ident) not in session.engagements._owners
    opened.assert_called_once_with(ident, store.store_dir)
    candidate_kb.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_resume_fit_ignores_current_conversation_ledger_and_token_calibration(
    session, monkeypatch
):
    await session.initialize()
    session._apply_system_prompt_if_needed()
    session.tools = []
    session.token_tracker.record_usage(200_000)
    old_ledger = Mock(serialize=AsyncMock(return_value="x" * 200_000))
    session.findings_ledger = old_ledger
    monkeypatch.setattr(
        session.provider, "get_max_context_tokens", AsyncMock(return_value=16_000)
    )
    store, ident = saved(session)
    original = (store.store_dir / f"{ident}.jsonl").read_bytes()

    result = await request(session, "resume", engagement_id=ident)

    assert result["engagement_id"] == ident
    old_ledger.serialize.assert_not_awaited()
    assert session.token_tracker.total_tokens == 0
    assert session.token_tracker.cumulative_tokens == 0
    assert session.provider.requests == []
    assert (store.store_dir / f"{ident}.jsonl").read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["cancel", "timeout", "disconnect"])
async def test_abandoned_resume_preflight_closes_candidate_and_preserves_history(
    session, monkeypatch, finish
):
    await session.initialize()
    session._apply_system_prompt_if_needed()
    current_id = session.engagement_store.engagement_id
    history = session.conversation_history
    store, ident = saved(session)
    path = store.store_dir / f"{ident}.jsonl"
    original = path.read_bytes()
    engagement_runtime.kb_path_for(store.store_dir, ident).touch()
    started, release = asyncio.Event(), asyncio.Event()

    async def serialize():
        started.set()
        await release.wait()
        return ""

    close_started, release_close = threading.Event(), threading.Event()

    def close():
        close_started.set()
        if finish == "cancel":
            # Model a cancelled SQLite worker still owning the KB lock. The
            # event loop must remain free to release it while close waits.
            assert release_close.wait(2), "KB close blocked the event loop"

    candidate_kb = Mock(close=Mock(side_effect=close))
    monkeypatch.setattr(
        engagement_runtime, "open_knowledge_base", Mock(return_value=candidate_kb)
    )
    monkeypatch.setattr(
        engagement_runtime,
        "FindingsLedger",
        Mock(return_value=Mock(serialize=serialize)),
    )
    if finish == "timeout":
        monkeypatch.setattr(
            engagement_runtime, "PROVIDER_REQUEST_TIMEOUT_SECONDS", 0.05
        )
    task = asyncio.create_task(
        session.handle_engagement_message(
            {
                "type": "engagement.resume",
                "request_id": "preflight",
                "context_id": session.engagements.context_id,
                "engagement_id": ident,
            }
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if finish == "cancel":
            task.cancel()
            assert await asyncio.wait_for(asyncio.to_thread(close_started.wait, 1), 1.5)
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif finish == "disconnect":
            session.close()
            release.set()
            await asyncio.wait_for(task, 1)
        else:
            await asyncio.wait_for(task, 1)
            events = []
            while not session.bus._queue.empty():
                events.append(await session.bus.next_event())
            assert "timed out" in events[-1]["payload"]["detail"]
    finally:
        release.set()
        release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert session.engagement_store.engagement_id == current_id
    assert session.conversation_history is history
    assert session.provider.requests == []
    assert path.read_bytes() == original
    assert session.engagements._key(ident) not in session.engagements._owners
    candidate_kb.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_resume_accepts_history_above_compaction_threshold(session, monkeypatch):
    await session.initialize()
    session.tools = []
    monkeypatch.setattr(
        session.provider, "get_max_context_tokens", AsyncMock(return_value=200_000)
    )
    monkeypatch.setattr(
        session.provider, "get_max_output_tokens", AsyncMock(return_value=8000)
    )
    text = "a" * 2720
    store, ident = saved(
        session,
        messages=[Message(role="system", content="context")]
        + [
            Message(
                role="assistant",
                content=text,
                raw_blocks=[{"type": "text", "text": text}],
            )
            for _ in range(100)
        ],
    )
    original = (store.store_dir / f"{ident}.jsonl").read_bytes()
    result = await request(session, "resume", engagement_id=ident)
    assert result["engagement_id"] == ident
    assert session.provider.requests == []
    assert (store.store_dir / f"{ident}.jsonl").read_bytes() == original


@pytest.mark.asyncio
async def test_rejected_resume_does_not_create_missing_kb(session, monkeypatch):
    await session.initialize()
    session.tools = []
    monkeypatch.setattr(
        session.provider, "get_max_context_tokens", AsyncMock(return_value=1000)
    )
    store, ident = saved(session, messages=[Message(role="system", content="x" * 6000)])
    path = engagement_runtime.kb_path_for(store.store_dir, ident)
    result = await request(session, "resume", engagement_id=ident)
    assert "too large" in result["detail"]
    assert not path.exists()
    assert not list(path.parent.glob(path.name + "*"))
