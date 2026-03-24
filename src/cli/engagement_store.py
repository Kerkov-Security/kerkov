"""
Engagement persistence using JSONL format.

Each engagement is stored as a .jsonl file where:
- Line 1: metadata (id, created_at, updated_at, mode, model, summary)
- Lines 2+: messages in order (role, content, call_id, timestamp)
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from src.core.logging.logger_factory import get_logger
from src.models.basic_models import Message

logger = get_logger("EngagementStore")

DEFAULT_STORE_DIR = Path.home() / ".kerkov" / "engagements"


def _serialize_raw_blocks(raw_blocks: Optional[Any]) -> Optional[List[Any]]:
    """Serialize provider-specific raw content blocks to JSON-safe dicts.

    Handles Anthropic SDK objects (TextBlock, ToolUseBlock) which are Pydantic
    models with .model_dump(). Plain dicts pass through unchanged. The Anthropic
    API accepts both SDK objects and equivalent dicts, so round-tripping works.
    """
    if raw_blocks is None:
        return None

    if not isinstance(raw_blocks, list):
        return None

    serialized: List[Any] = []
    for block in raw_blocks:
        if hasattr(block, "model_dump"):
            serialized.append(block.model_dump())
        elif isinstance(block, dict):
            serialized.append(block)
        else:
            logger.warning(f"Skipping non-serializable raw_block: {type(block)}")
    return serialized if serialized else None


class EngagementMetadata(BaseModel):
    """Metadata header for an engagement file.

    Timestamps (created_at, updated_at) are stored as ISO 8601 UTC strings
    (e.g. "2026-03-23T14:30:00.123456+00:00") for consistent sorting and
    human-readable display. Displayed to users as "YYYY-MM-DD HH:MM:SS" UTC.
    """
    id: str
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC
    mode: str
    model: str
    summary: str = ""

class EngagementEntry(BaseModel):
    """A single message entry in the engagement file."""
    type: str = "message"
    role: str
    content: str
    call_id: Optional[str] = None
    raw_blocks: Optional[List[Any]] = None
    timestamp: str  # ISO 8601 UTC

class EngagementInfo(BaseModel):
    """Summary info for listing engagements.

    Timestamps are ISO 8601 UTC strings. When displaying to users, callers
    should format as "YYYY-MM-DD HH:MM:SS" by slicing [:19] and replacing
    'T' with a space (e.g. "2026-03-23T14:30:00" -> "2026-03-23 14:30:00").
    """
    id: str
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC
    mode: str
    model: str
    summary: str
    message_count: int

class EngagementStore:
    """Manages engagement persistence using JSONL files."""

    def __init__(self, store_dir: Optional[Path] = None):
        self.store_dir = store_dir or DEFAULT_STORE_DIR
        self._ensure_dir()
        self._engagement_id: Optional[str] = None
        self._message_count: int = 0

    @property
    def engagement_id(self) -> Optional[str]:
        return self._engagement_id

    def _ensure_dir(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _engagement_path(self, engagement_id: str) -> Path:
        return self.store_dir / f"{engagement_id}.jsonl"

    def start_engagement(self, mode: str, model: str) -> str:
        """Start a new engagement. Returns the engagement ID."""
        self._engagement_id = uuid.uuid4().hex[:12]
        self._message_count = 0
        now = datetime.now(timezone.utc).isoformat()

        metadata = EngagementMetadata(
            id=self._engagement_id,
            created_at=now,
            updated_at=now,
            mode=mode,
            model=model,
        )

        path = self._engagement_path(self._engagement_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "metadata", **metadata.model_dump()}) + "\n")

        logger.info(f"Started engagement: {self._engagement_id}")
        return self._engagement_id

    def append_message(self, message: Message) -> None:
        """Append a message to the current engagement file."""
        if not self._engagement_id:
            return

        now = datetime.now(timezone.utc).isoformat()
        entry = EngagementEntry(
            role=message.role,
            content=message.content,
            call_id=message.call_id,
            raw_blocks=_serialize_raw_blocks(message.raw_blocks),
            timestamp=now,
        )

        path = self._engagement_path(self._engagement_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.model_dump()) + "\n")

        self._message_count += 1
        self._update_metadata(updated_at=now)

    def _update_metadata(
        self,
        updated_at: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Update the metadata line (first line) of the current engagement."""
        if not self._engagement_id:
            return

        path = self._engagement_path(self._engagement_id)
        if not path.exists():
            return

        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return

        metadata = json.loads(lines[0])
        if updated_at:
            metadata["updated_at"] = updated_at
        if summary:
            metadata["summary"] = summary

        lines[0] = json.dumps(metadata)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def update_summary(self, summary: str) -> None:
        """Update the engagement summary in metadata."""
        self._update_metadata(summary=summary)

    def load_engagement(self, engagement_id: str) -> Optional[List[Message]]:
        """Load messages from an engagement file."""
        path = self._engagement_path(engagement_id)
        if not path.exists():
            logger.warning(f"Engagement file not found: {engagement_id}")
            return None

        messages: List[Message] = []
        lines = path.read_text(encoding="utf-8").splitlines()

        for line in lines[1:]:  # Skip metadata line
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("type") == "message":
                messages.append(
                    Message(
                        role=entry["role"],
                        content=entry["content"],
                        call_id=entry.get("call_id"),
                        raw_blocks=entry.get("raw_blocks"),
                    )
                )

        self._engagement_id = engagement_id
        self._message_count = len(messages)
        logger.info(f"Loaded engagement {engagement_id} with {len(messages)} messages")
        return messages

    def get_latest_engagement_id(self) -> Optional[str]:
        """Get the most recently updated engagement ID."""
        engagements = self.list_engagements(limit=1)
        if not engagements:
            return None
        return engagements[0].id

    def list_engagements(self, limit: int) -> List[EngagementInfo]:
        """List saved engagements, most recent first."""
        engagements: List[EngagementInfo] = []

        for path in self.store_dir.glob("*.jsonl"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue

                metadata = json.loads(first_line)
                if metadata.get("type") != "metadata":
                    continue

                line_count = sum(1 for _ in open(path, "r", encoding="utf-8")) - 1

                engagements.append(
                    EngagementInfo(
                        id=metadata["id"],
                        created_at=metadata["created_at"],
                        updated_at=metadata["updated_at"],
                        mode=metadata.get("mode", "unknown"),
                        model=metadata.get("model", "unknown"),
                        summary=metadata.get("summary", ""),
                        message_count=max(0, line_count),
                    )
                )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Skipping malformed engagement file {path.name}: {e}")
                continue

        engagements.sort(key=lambda s: s.updated_at, reverse=True)
        return engagements[:limit]

    def delete_engagement(self, engagement_id: str) -> bool:
        """Delete an engagement file."""
        path = self._engagement_path(engagement_id)
        if path.exists():
            path.unlink()
            logger.info(f"Deleted engagement: {engagement_id}")
            if self._engagement_id == engagement_id:
                self._engagement_id = None
                self._message_count = 0
            return True
        return False
