"""Tests for EngagementStore JSONL persistence."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from kerkov.core.engagement.engagement_store import (
    EngagementEntry,
    EngagementInfo,
    EngagementMetadata,
    EngagementStore,
    _serialize_raw_blocks,
)
from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    kb_path_for,
)
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.core.knowledge_base.models import LedgerSection
from kerkov.models.basic_models import Message
from tests import PRODUCTION_STORE_DIR


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_message(
    role: str = "user",
    content: str = "hello",
    call_id: str = None,
    raw_blocks=None,
) -> Message:
    return Message(role=role, content=content, call_id=call_id, raw_blocks=raw_blocks)


def _read_jsonl(path: Path):
    """Return parsed JSON objects from a JSONL file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# _serialize_raw_blocks
# ---------------------------------------------------------------------------


class SerializeRawBlocksTest(unittest.TestCase):

    def test_none_returns_none(self):
        assert _serialize_raw_blocks(None) is None

    def test_non_list_returns_none(self):
        assert _serialize_raw_blocks("not a list") is None

    def test_empty_list_returns_none(self):
        assert _serialize_raw_blocks([]) is None

    def test_dict_blocks_pass_through(self):
        blocks = [{"type": "text", "text": "hi"}]
        result = _serialize_raw_blocks(blocks)
        assert result == blocks

    def test_pydantic_model_dump(self):
        mock_block = MagicMock()
        mock_block.model_dump.return_value = {"type": "text", "text": "hi"}
        result = _serialize_raw_blocks([mock_block])
        assert result == [{"type": "text", "text": "hi"}]
        mock_block.model_dump.assert_called_once()

    def test_non_serializable_blocks_skipped(self):
        result = _serialize_raw_blocks([42, "string"])
        assert result is None

    def test_mixed_blocks(self):
        mock_block = MagicMock()
        mock_block.model_dump.return_value = {"type": "tool_use", "id": "1"}
        blocks = [mock_block, {"type": "text", "text": "ok"}, 999]
        result = _serialize_raw_blocks(blocks)
        assert len(result) == 2
        assert result[0] == {"type": "tool_use", "id": "1"}
        assert result[1] == {"type": "text", "text": "ok"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class EngagementModelsTest(unittest.TestCase):

    def test_metadata_summary_defaults_empty(self):
        meta = EngagementMetadata(
            id="abc",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            mode="audit",
            model="claude",
            verbosity="standard",
            approval_mode="normal",
        )
        assert meta.summary == ""

    def test_entry_defaults(self):
        entry = EngagementEntry(
            role="user",
            content="hi",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert entry.type == "message"
        assert entry.call_id is None
        assert entry.raw_blocks is None

    def test_engagement_info_fields(self):
        info = EngagementInfo(
            id="abc",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            mode="audit",
            model="claude",
            verbosity="aggressive",
            approval_mode="permissive",
            summary="test",
            message_count=5,
        )
        assert info.message_count == 5
        assert info.summary == "test"
        assert info.verbosity == "aggressive"
        assert info.approval_mode == "permissive"


# ---------------------------------------------------------------------------
# Engagement lifecycle — start, append, update_summary
# ---------------------------------------------------------------------------


class EngagementLifecycleTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)
        self.eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )

    def tearDown(self):
        self._tmp.cleanup()

    # -- start_engagement ---

    def test_start_engagement_creates_file(self):
        assert self.eid is not None
        assert self.store.engagement_id == self.eid
        assert (self.store_dir / f"{self.eid}.jsonl").exists()

    def test_start_engagement_writes_metadata(self):
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert len(records) == 1
        meta = records[0]
        assert meta["type"] == "metadata"
        assert meta["id"] == self.eid
        assert meta["mode"] == "audit"
        assert meta["model"] == "claude"
        assert meta["summary"] == ""
        assert "created_at" in meta
        assert "updated_at" in meta

    def test_start_engagement_resets_message_count(self):
        assert self.store._message_count == 0

    # -- append_message ---

    def test_append_writes_entry(self):
        self.store.append_message(_create_message(role="user", content="hello"))
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert len(records) == 2
        entry = records[1]
        assert entry["type"] == "message"
        assert entry["role"] == "user"
        assert entry["content"] == "hello"
        assert "timestamp" in entry

    def test_append_increments_count(self):
        self.store.append_message(_create_message())
        self.store.append_message(_create_message())
        assert self.store._message_count == 2

    def test_append_updates_metadata_timestamp(self):
        self.store.append_message(_create_message())
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        meta = records[0]
        assert meta["updated_at"] >= meta["created_at"]

    def test_append_without_engagement_is_noop(self):
        store = EngagementStore(store_dir=self.store_dir)
        store.append_message(_create_message())
        assert store._message_count == 0

    def test_append_with_call_id_and_raw_blocks(self):
        msg = _create_message(
            role="tool",
            content="result",
            call_id="call_123",
            raw_blocks=[{"type": "text", "text": "result"}],
        )
        self.store.append_message(msg)
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        entry = records[1]
        assert entry["call_id"] == "call_123"
        assert entry["raw_blocks"] == [{"type": "text", "text": "result"}]

    def test_append_multiple_preserves_order(self):
        for i in range(5):
            self.store.append_message(_create_message(content=f"msg-{i}"))
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        messages = [r for r in records if r["type"] == "message"]
        assert len(messages) == 5
        for i, msg in enumerate(messages):
            assert msg["content"] == f"msg-{i}"

    # -- update_summary ---

    def test_update_summary(self):
        self.store.update_summary("Discussed nmap scanning")
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert records[0]["summary"] == "Discussed nmap scanning"

    def test_update_summary_preserves_messages(self):
        self.store.append_message(_create_message(content="hi"))
        self.store.update_summary("Test summary")
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert len(records) == 2
        assert records[0]["summary"] == "Test summary"
        assert records[1]["content"] == "hi"

    # -- update_verbosity / update_approval_mode ---

    def test_update_verbosity(self):
        self.store.update_verbosity("silent")
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert records[0]["verbosity"] == "silent"

    def test_update_approval_mode(self):
        self.store.update_approval_mode("permissive")
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert records[0]["approval_mode"] == "permissive"

    def test_update_verbosity_preserves_messages(self):
        self.store.append_message(_create_message(content="hi"))
        self.store.update_verbosity("aggressive")
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert len(records) == 2
        assert records[0]["verbosity"] == "aggressive"
        assert records[1]["content"] == "hi"

    # -- start_engagement persists verbosity + approval_mode ---

    def test_start_engagement_writes_axes(self):
        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert records[0]["verbosity"] == "standard"
        assert records[0]["approval_mode"] == "normal"


# ---------------------------------------------------------------------------
# load_engagement
# ---------------------------------------------------------------------------


class LoadEngagementTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_messages_and_metadata(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.append_message(_create_message(role="user", content="hello"))
        self.store.append_message(_create_message(role="assistant", content="hi back"))

        store2 = EngagementStore(store_dir=self.store_dir)
        engagement = store2.load_engagement(eid)

        assert engagement is not None
        assert len(engagement.messages) == 2
        assert engagement.messages[0].role == "user"
        assert engagement.messages[0].content == "hello"
        assert engagement.messages[1].role == "assistant"
        assert engagement.messages[1].content == "hi back"
        assert engagement.metadata.mode.lower() == "audit"
        assert engagement.metadata.verbosity == "standard"
        assert engagement.metadata.approval_mode == "normal"

    def test_load_axes_after_update(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="guided", approval_mode="strict"
        )
        self.store.update_verbosity("aggressive")
        self.store.update_approval_mode("permissive")

        store2 = EngagementStore(store_dir=self.store_dir)
        engagement = store2.load_engagement(eid)
        assert engagement.metadata.verbosity == "aggressive"
        assert engagement.metadata.approval_mode == "permissive"

    def test_load_sets_internal_state(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.append_message(_create_message())

        store2 = EngagementStore(store_dir=self.store_dir)
        store2.load_engagement(eid)
        assert store2.engagement_id == eid
        assert store2._message_count == 1

    def test_load_nonexistent_returns_none(self):
        assert self.store.load_engagement("nonexistent") is None

    def test_load_preserves_call_id_and_raw_blocks(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.append_message(
            _create_message(
                role="tool",
                content="ok",
                call_id="call_abc",
                raw_blocks=[{"type": "text", "text": "ok"}],
            )
        )

        store2 = EngagementStore(store_dir=self.store_dir)
        engagement = store2.load_engagement(eid)
        assert engagement.messages[0].call_id == "call_abc"
        assert engagement.messages[0].raw_blocks == [{"type": "text", "text": "ok"}]


# ---------------------------------------------------------------------------
# Compaction provenance (summary_level / artifact_ref)
# ---------------------------------------------------------------------------


class CompactionProvenanceTest(unittest.TestCase):
    """The marks must survive a round trip, or /resume re-summarizes summaries."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)
        self.eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _marked_history(self):
        return [
            Message(role="assistant", content="[CONTEXT SUMMARY]", summary_level=1),
            Message(role="tool", content="out", call_id="c1", artifact_ref=17),
            Message(role="user", content="plain"),
        ]

    def _reload(self):
        return EngagementStore(store_dir=self.store_dir).load_engagement(self.eid)

    def test_append_round_trips_both_fields(self):
        for message in self._marked_history():
            self.store.append_message(message)

        messages = self._reload().messages
        assert messages[0].summary_level == 1
        assert messages[1].artifact_ref == 17
        assert messages[2].summary_level is None
        assert messages[2].artifact_ref is None

    def test_override_round_trips_both_fields(self):
        self.store.append_message(_create_message(content="old"))

        self.store.override_engagement_messages(self._marked_history())

        messages = self._reload().messages
        assert messages[0].summary_level == 1
        assert messages[1].artifact_ref == 17

    def test_legacy_entry_without_the_fields_loads_as_none(self):
        path = self.store_dir / f"{self.eid}.jsonl"
        legacy = {
            "type": "message",
            "role": "assistant",
            "content": "written before compaction provenance existed",
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(legacy) + "\n")

        message = self._reload().messages[0]
        assert message.summary_level is None
        assert message.artifact_ref is None

    def test_partial_flag_round_trips(self):
        self.store.append_message(
            Message(role="assistant", content="cut off here", partial=True)
        )
        self.store.append_message(Message(role="user", content="whole"))

        messages = self._reload().messages
        assert messages[0].partial is True
        assert messages[1].partial is False


# ---------------------------------------------------------------------------
# list_engagements & get_latest_engagement_id
# ---------------------------------------------------------------------------


class ListEngagementsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_empty(self):
        assert self.store.list_engagements(limit=10) == []

    def test_list_returns_info(self):
        eid = self.store.start_engagement(
            mode="ctf", model="gemini", verbosity="guided", approval_mode="strict"
        )
        self.store.append_message(_create_message())
        self.store.update_summary("CTF challenge")

        engagements = self.store.list_engagements(limit=10)
        assert len(engagements) == 1
        info = engagements[0]
        assert info.id == eid
        assert info.mode == "ctf"
        assert info.model == "gemini"
        assert info.summary == "CTF challenge"
        assert info.message_count == 1

    def test_list_sorted_most_recent_first(self):
        eid1 = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.append_message(_create_message(content="first"))

        eid2 = self.store.start_engagement(
            mode="defense",
            model="claude",
            verbosity="aggressive",
            approval_mode="permissive",
        )
        self.store.append_message(_create_message(content="second"))

        engagements = self.store.list_engagements(limit=10)
        assert engagements[0].id == eid2
        assert engagements[1].id == eid1

    def test_list_respects_limit(self):
        for _ in range(5):
            self.store.start_engagement(
                mode="audit",
                model="claude",
                verbosity="standard",
                approval_mode="normal",
            )

        engagements = self.store.list_engagements(limit=3)
        assert len(engagements) == 3

    def test_list_skips_malformed_files(self):
        bad_path = self.store_dir / "garbage.jsonl"
        bad_path.write_text("not valid json\n", encoding="utf-8")

        self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        engagements = self.store.list_engagements(limit=10)
        assert len(engagements) == 1

    def test_get_latest_engagement_id_none_when_empty(self):
        assert self.store.get_latest_engagement_id() is None

    def test_get_latest_engagement_id_returns_most_recent(self):
        self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        eid2 = self.store.start_engagement(
            mode="defense",
            model="claude",
            verbosity="aggressive",
            approval_mode="permissive",
        )
        assert self.store.get_latest_engagement_id() == eid2

    def test_get_latest_engagement_id_ignores_agent_runs(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id=eid,
        )

        assert self.store.get_latest_engagement_id() == eid

    def test_get_latest_engagement_id_none_when_only_agent_runs(self):
        self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
        )

        assert self.store.get_latest_engagement_id() is None


# ---------------------------------------------------------------------------
# override_engagement_messages
# ---------------------------------------------------------------------------


class OverrideEngagementMessagesTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)
        self.eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_replaces_existing_messages(self):
        self.store.append_message(_create_message(content="old-1"))
        self.store.append_message(_create_message(content="old-2"))
        self.store.append_message(_create_message(content="old-3"))

        new_messages = [
            _create_message(role="assistant", content="[CONTEXT SUMMARY] compacted"),
            _create_message(role="user", content="new-1"),
        ]
        self.store.override_engagement_messages(new_messages)

        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        messages = [r for r in records if r["type"] == "message"]
        assert len(messages) == 2
        assert messages[0]["content"] == "[CONTEXT SUMMARY] compacted"
        assert messages[0]["role"] == "assistant"
        assert messages[1]["content"] == "new-1"

    def test_preserves_metadata(self):
        self.store.update_summary("original summary")
        self.store.append_message(_create_message(content="old"))

        self.store.override_engagement_messages([_create_message(content="new")])

        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        meta = records[0]
        assert meta["type"] == "metadata"
        assert meta["id"] == self.eid
        assert meta["mode"] == "audit"
        assert meta["model"] == "claude"
        assert meta["verbosity"] == "standard"
        assert meta["approval_mode"] == "normal"
        assert meta["summary"] == "original summary"

    def test_updates_updated_at_timestamp(self):
        records_before = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        original_updated_at = records_before[0]["updated_at"]

        self.store.override_engagement_messages([_create_message(content="new")])

        records_after = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        assert records_after[0]["updated_at"] >= original_updated_at

    def test_updates_message_count(self):
        self.store.append_message(_create_message(content="old-1"))
        self.store.append_message(_create_message(content="old-2"))
        self.store.append_message(_create_message(content="old-3"))
        assert self.store._message_count == 3

        self.store.override_engagement_messages(
            [
                _create_message(content="new-1"),
                _create_message(content="new-2"),
            ]
        )

        assert self.store._message_count == 2

    def test_override_with_empty_list_removes_all_messages(self):
        self.store.append_message(_create_message(content="old-1"))
        self.store.append_message(_create_message(content="old-2"))

        self.store.override_engagement_messages([])

        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        messages = [r for r in records if r["type"] == "message"]
        assert len(messages) == 0
        assert self.store._message_count == 0

    def test_override_without_engagement_is_noop(self):
        store = EngagementStore(store_dir=self.store_dir)
        store.override_engagement_messages([_create_message()])
        assert store._message_count == 0

    def test_preserves_call_id_and_raw_blocks(self):
        new_messages = [
            _create_message(
                role="tool",
                content="result",
                call_id="call_abc",
                raw_blocks=[{"type": "text", "text": "result"}],
            ),
        ]
        self.store.override_engagement_messages(new_messages)

        records = _read_jsonl(self.store_dir / f"{self.eid}.jsonl")
        entry = records[1]
        assert entry["call_id"] == "call_abc"
        assert entry["raw_blocks"] == [{"type": "text", "text": "result"}]

    def test_override_roundtrips_via_load(self):
        self.store.append_message(_create_message(content="old"))
        self.store.override_engagement_messages(
            [
                _create_message(role="assistant", content="summary"),
                _create_message(role="user", content="continue"),
            ]
        )

        store2 = EngagementStore(store_dir=self.store_dir)
        engagement = store2.load_engagement(self.eid)

        assert engagement is not None
        assert len(engagement.messages) == 2
        assert engagement.messages[0].role == "assistant"
        assert engagement.messages[0].content == "summary"
        assert engagement.messages[1].role == "user"
        assert engagement.messages[1].content == "continue"


# ---------------------------------------------------------------------------
# delete_engagement
# ---------------------------------------------------------------------------


class DeleteEngagementTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_delete_existing_engagement(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        assert self.store.delete_engagement(eid) is True
        assert not (self.store_dir / f"{eid}.jsonl").exists()
        assert self.store.engagement_id is None
        assert self.store._message_count == 0

    def test_delete_nonexistent_returns_false(self):
        assert self.store.delete_engagement("nonexistent") is False

    def test_delete_other_engagement_keeps_current(self):
        eid1 = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        eid2 = self.store.start_engagement(
            mode="defense",
            model="claude",
            verbosity="aggressive",
            approval_mode="permissive",
        )
        self.store.delete_engagement(eid1)
        assert self.store.engagement_id == eid2


# ---------------------------------------------------------------------------
# Knowledge base parity on delete
# ---------------------------------------------------------------------------


class DeleteKnowledgeBaseTest(unittest.TestCase):
    """Deleting an engagement takes its artifacts, and nothing else's."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _start_root(self) -> str:
        return self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )

    def _start_agent(self, parent_id: str) -> str:
        return self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id=parent_id,
            agent_type="recon",
        )

    def _seed_kb(self, root_id: str, engagement_ids) -> EngagementKnowledgeBase:
        kb = EngagementKnowledgeBase(root_id, self.store_dir)
        for eid in engagement_ids:
            _run(kb.add_artifact(engagement_id=eid, body=f"tomcat from {eid}"))
        kb.close()
        return kb

    def test_deleting_a_root_removes_its_knowledge_base_file(self):
        root_id = self._start_root()
        self._seed_kb(root_id, [root_id])

        self.store.delete_engagement(root_id)

        assert not kb_path_for(self.store_dir, root_id).exists()

    def test_deleting_an_agent_keeps_the_shared_file_and_drops_only_its_rows(self):
        root_id = self._start_root()
        agent_id = self._start_agent(root_id)
        self._seed_kb(root_id, [root_id, agent_id])

        self.store.delete_engagement(agent_id)

        # The file is the master's; taking it would delete the master's
        # engagement along with the sub-run.
        assert kb_path_for(self.store_dir, root_id).exists()
        kb = EngagementKnowledgeBase(root_id, self.store_dir)
        try:
            assert _run(kb.count(agent_id)) == 0
            assert _run(kb.count(root_id)) == 1
        finally:
            kb.close()

    def _seed_ledger(self, root_id: str, engagement_ids) -> None:
        kb = EngagementKnowledgeBase(root_id, self.store_dir)
        ledger = FindingsLedger(kb)
        for eid in engagement_ids:
            _run(
                ledger.add(
                    LedgerSection.HOSTS, {"ip": f"10.0.0.{eid[-1]}"}, engagement_id=eid
                )
            )
        kb.close()

    def test_deleting_an_agent_drops_its_ledger_rows_only(self):
        root_id = self._start_root()
        agent_id = self._start_agent(root_id)
        self._seed_ledger(root_id, [root_id, agent_id])

        self.store.delete_engagement(agent_id)

        assert kb_path_for(self.store_dir, root_id).exists()
        kb = EngagementKnowledgeBase(root_id, self.store_dir)
        ledger = FindingsLedger(kb)
        try:
            engagement_ids = {e.engagement_id for e in _run(ledger.list())}
            assert engagement_ids == {root_id}
        finally:
            kb.close()

    def test_deleting_a_root_removes_the_ledger_with_the_file(self):
        root_id = self._start_root()
        self._seed_ledger(root_id, [root_id])

        self.store.delete_engagement(root_id)

        assert not kb_path_for(self.store_dir, root_id).exists()

    def test_delete_succeeds_when_no_knowledge_base_was_ever_created(self):
        root_id = self._start_root()

        assert self.store.delete_engagement(root_id) is True

    def test_parent_lookup_reads_the_link_without_loading_messages(self):
        root_id = self._start_root()
        agent_id = self._start_agent(root_id)

        assert self.store.parent_engagement_id_of(agent_id) == root_id
        assert self.store.parent_engagement_id_of(root_id) is None
        assert self.store.parent_engagement_id_of("missing") is None

    def test_read_metadata_returns_none_for_a_missing_engagement(self):
        assert self.store.read_metadata("nope") is None


# ---------------------------------------------------------------------------
# Default store directory
# ---------------------------------------------------------------------------


class DefaultStoreDirTest(unittest.TestCase):

    def test_default_dir_is_home_kerkov(self):
        # The live constant is redirected for test isolation; assert the
        # shipped default, which is what production uses.
        assert PRODUCTION_STORE_DIR == Path.home() / ".kerkov" / "engagements"


# ---------------------------------------------------------------------------
# Agent engagements — link fields and `kind`
# ---------------------------------------------------------------------------


class AgentEngagementTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_kind_defaults_to_conversation(self):
        eid = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        loaded = self.store.load_engagement(eid)
        assert loaded.metadata.kind == "conversation"
        assert loaded.metadata.parent_engagement_id is None
        assert loaded.metadata.agent_type is None

    def test_agent_engagement_persists_link_fields(self):
        eid = self.store.start_engagement(
            mode="agent",
            model="claude",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id="parent123",
            parent_call_id="call456",
            agent_type="recon",
            task_instruction="scan target X",
        )

        store2 = EngagementStore(store_dir=self.store_dir)
        loaded = store2.load_engagement(eid)
        assert loaded.metadata.kind == "agent"
        assert loaded.metadata.parent_engagement_id == "parent123"
        assert loaded.metadata.parent_call_id == "call456"
        assert loaded.metadata.agent_type == "recon"
        assert loaded.metadata.task_instruction == "scan target X"

    def test_count_by_kind_returns_correct_counts(self):
        self.store.start_engagement(
            mode="audit", model="m", verbosity="standard", approval_mode="normal"
        )
        self.store.start_engagement(
            mode="audit", model="m", verbosity="standard", approval_mode="normal"
        )
        self.store.start_engagement(
            mode="agent",
            model="m",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id="p",
            parent_call_id="c",
            agent_type="recon",
            task_instruction="scan",
        )

        self.assertEqual(self.store.count_by_kind("conversation"), 2)
        self.assertEqual(self.store.count_by_kind("agent"), 1)
        self.assertEqual(self.store.count_by_kind("other"), 0)

    def test_count_by_kind_covers_all_files_beyond_list_limit(self):
        # Create more agent files than the display limit (40) to confirm
        # count_by_kind is not subject to the same window.
        for _ in range(45):
            self.store.start_engagement(
                mode="agent",
                model="m",
                verbosity="silent",
                approval_mode="auto",
                kind="agent",
                parent_engagement_id="p",
                parent_call_id="c",
                agent_type="recon",
                task_instruction="t",
            )

        self.assertEqual(self.store.count_by_kind("agent"), 45)
        # list_engagements(limit=40) truncates — count_by_kind must not
        raw = self.store.list_engagements(limit=40)
        self.assertEqual(sum(1 for e in raw if e.kind == "agent"), 40)

    def test_kind_filter_selects_before_the_limit_is_applied(self):
        """Otherwise a directory of agent runs pushes every conversation out."""
        conversation = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        for _ in range(15):
            self.store.start_engagement(
                mode="agent",
                model="m",
                verbosity="silent",
                approval_mode="auto",
                kind="agent",
                parent_engagement_id=conversation,
                parent_call_id="call-1",
                agent_type="recon",
                task_instruction="scan 10.0.0.5",
            )

        conversations = self.store.list_engagements(limit=10, kind="conversation")

        self.assertEqual([e.id for e in conversations], [conversation])

    def test_agent_runs_carry_their_parent_and_task(self):
        parent = self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.start_engagement(
            mode="agent",
            model="m",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id=parent,
            parent_call_id="call-1",
            agent_type="recon",
            task_instruction="scan 10.0.0.5",
        )

        runs = self.store.list_engagements(limit=10, kind="agent")

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].parent_engagement_id, parent)
        self.assertEqual(runs[0].agent_type, "recon")
        self.assertEqual(runs[0].task_instruction, "scan 10.0.0.5")

    def test_no_kind_filter_still_lists_everything(self):
        self.store.start_engagement(
            mode="audit", model="claude", verbosity="standard", approval_mode="normal"
        )
        self.store.start_engagement(
            mode="agent",
            model="m",
            verbosity="silent",
            approval_mode="auto",
            kind="agent",
            parent_engagement_id="p",
            parent_call_id="c",
            agent_type="recon",
            task_instruction="t",
        )

        self.assertEqual(len(self.store.list_engagements(limit=10)), 2)

    def test_legacy_file_without_link_fields_still_loads(self):
        # Write a metadata header that predates the link fields, then load.
        legacy_meta = {
            "type": "metadata",
            "id": "legacyfile01",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "mode": "audit",
            "model": "claude",
            "verbosity": "standard",
            "approval_mode": "normal",
            "summary": "old run",
        }
        path = self.store_dir / "legacyfile01.jsonl"
        path.write_text(json.dumps(legacy_meta) + "\n", encoding="utf-8")

        loaded = self.store.load_engagement("legacyfile01")
        assert loaded is not None
        assert loaded.metadata.kind == "conversation"
        assert loaded.metadata.parent_engagement_id is None


if __name__ == "__main__":
    unittest.main()
