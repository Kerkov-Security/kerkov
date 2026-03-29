"""Tests for EngagementStore JSONL persistence."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.cli.engagement_store import (
    EngagementEntry,
    EngagementInfo,
    EngagementMetadata,
    EngagementStore,
    DEFAULT_STORE_DIR,
    _serialize_raw_blocks,
)
from src.models.basic_models import Message


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
            summary="test",
            message_count=5,
        )
        assert info.message_count == 5
        assert info.summary == "test"


# ---------------------------------------------------------------------------
# Engagement lifecycle — start, append, update_summary
# ---------------------------------------------------------------------------

class EngagementLifecycleTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name)
        self.store = EngagementStore(store_dir=self.store_dir)
        self.eid = self.store.start_engagement(mode="audit", model="claude")

    def tearDown(self):
        self._tmp.cleanup()

    # -- start_engagement ---

    def test_start_engagement_creates_file(self):
        assert self.eid is not None
        assert len(self.eid) == 12
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
        eid = self.store.start_engagement(mode="audit", model="claude")
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

    def test_load_sets_internal_state(self):
        eid = self.store.start_engagement(mode="audit", model="claude")
        self.store.append_message(_create_message())

        store2 = EngagementStore(store_dir=self.store_dir)
        store2.load_engagement(eid)
        assert store2.engagement_id == eid
        assert store2._message_count == 1

    def test_load_nonexistent_returns_none(self):
        assert self.store.load_engagement("nonexistent") is None

    def test_load_preserves_call_id_and_raw_blocks(self):
        eid = self.store.start_engagement(mode="audit", model="claude")
        self.store.append_message(_create_message(
            role="tool",
            content="ok",
            call_id="call_abc",
            raw_blocks=[{"type": "text", "text": "ok"}],
        ))

        store2 = EngagementStore(store_dir=self.store_dir)
        engagement = store2.load_engagement(eid)
        assert engagement.messages[0].call_id == "call_abc"
        assert engagement.messages[0].raw_blocks == [{"type": "text", "text": "ok"}]


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
        eid = self.store.start_engagement(mode="ctf", model="gemini")
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
        eid1 = self.store.start_engagement(mode="audit", model="claude")
        self.store.append_message(_create_message(content="first"))

        eid2 = self.store.start_engagement(mode="defense", model="claude")
        self.store.append_message(_create_message(content="second"))

        engagements = self.store.list_engagements(limit=10)
        assert engagements[0].id == eid2
        assert engagements[1].id == eid1

    def test_list_respects_limit(self):
        for _ in range(5):
            self.store.start_engagement(mode="audit", model="claude")

        engagements = self.store.list_engagements(limit=3)
        assert len(engagements) == 3

    def test_list_skips_malformed_files(self):
        bad_path = self.store_dir / "garbage.jsonl"
        bad_path.write_text("not valid json\n", encoding="utf-8")

        self.store.start_engagement(mode="audit", model="claude")
        engagements = self.store.list_engagements(limit=10)
        assert len(engagements) == 1

    def test_get_latest_engagement_id_none_when_empty(self):
        assert self.store.get_latest_engagement_id() is None

    def test_get_latest_engagement_id_returns_most_recent(self):
        self.store.start_engagement(mode="audit", model="claude")
        eid2 = self.store.start_engagement(mode="defense", model="claude")
        assert self.store.get_latest_engagement_id() == eid2


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
        eid = self.store.start_engagement(mode="audit", model="claude")
        assert self.store.delete_engagement(eid) is True
        assert not (self.store_dir / f"{eid}.jsonl").exists()
        assert self.store.engagement_id is None
        assert self.store._message_count == 0

    def test_delete_nonexistent_returns_false(self):
        assert self.store.delete_engagement("nonexistent") is False

    def test_delete_other_engagement_keeps_current(self):
        eid1 = self.store.start_engagement(mode="audit", model="claude")
        eid2 = self.store.start_engagement(mode="defense", model="claude")
        self.store.delete_engagement(eid1)
        assert self.store.engagement_id == eid2


# ---------------------------------------------------------------------------
# Default store directory
# ---------------------------------------------------------------------------

class DefaultStoreDirTest(unittest.TestCase):

    def test_default_dir_is_home_kerkov(self):
        assert DEFAULT_STORE_DIR == Path.home() / ".kerkov" / "engagements"


if __name__ == "__main__":
    unittest.main()
