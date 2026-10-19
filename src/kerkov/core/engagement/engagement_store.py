"""
Engagement persistence using JSONL format.

Each engagement is stored as a .jsonl file where:
- Line 1: metadata (id, created_at, updated_at, mode, model, summary)
- Lines 2+: messages in order (role, content, call_id, timestamp)
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, ValidationError

from kerkov.core.knowledge_base.engagement_kb import (
    EngagementKnowledgeBase,
    delete_kb_for_root,
    kb_path_for,
    resolve_root_engagement_id,
)
from kerkov.core.logging.logger_factory import get_logger
from kerkov.core.session.autonomy_manager import AutonomyLevel, preset_for
from kerkov.models.basic_models import Message, TokenUsage

logger = get_logger("EngagementStore")

DEFAULT_STORE_DIR = Path.home() / ".kerkov" / "engagements"


def _metadata_settings(metadata: dict[str, Any]) -> tuple[str, str]:
    """Restore split settings from the autonomy preset in older transcripts."""
    if "verbosity" in metadata and "approval_mode" in metadata:
        return metadata["verbosity"], metadata["approval_mode"]
    level = metadata.get("autonomy", AutonomyLevel.STANDARD)
    if isinstance(level, bool) or not isinstance(level, (int, str)):
        raise ValueError("Invalid saved autonomy preset")
    verbosity, approval = preset_for(AutonomyLevel(int(level)))
    return (
        metadata.get("verbosity", verbosity.value),
        metadata.get("approval_mode", approval.value),
    )


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
            dumped = block.model_dump()
        elif isinstance(block, dict):
            dumped = block
        else:
            logger.warning(f"Skipping non-serializable raw_block: {type(block)}")
            continue
        # Remove fields the Anthropic API rejects on input (e.g. parsed_output
        # added by the SDK on TextBlock responses).
        dumped.pop("parsed_output", None)
        serialized.append(dumped)
    return serialized if serialized else None


def _sanitize_raw_blocks(raw_blocks: Optional[List[Any]]) -> Optional[List[Any]]:
    """Strip fields the Anthropic API rejects from loaded raw blocks."""
    if not raw_blocks:
        return raw_blocks
    for block in raw_blocks:
        if isinstance(block, dict):
            block.pop("parsed_output", None)
    return raw_blocks


class EngagementMetadata(BaseModel):
    """Metadata header for an engagement file.

    Timestamps (created_at, updated_at) are stored as ISO 8601 UTC strings
    (e.g. "2026-03-23T14:30:00.123456+00:00") for consistent sorting and
    human-readable display. Displayed to users as "YYYY-MM-DD HH:MM:SS" UTC.

    Agent runs reuse this same metadata shape (`kind="agent"`) and link back
    to their parent via `parent_engagement_id` / `parent_call_id`. Existing
    pre-agent files parse cleanly because every link field is optional.
    """

    id: str
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC
    mode: str
    model: str
    verbosity: str
    approval_mode: str
    summary: str = ""
    kind: str = "conversation"  # "conversation" | "agent"
    parent_engagement_id: Optional[str] = None
    parent_call_id: Optional[str] = None  # the SpawnAgentTool call_id
    agent_type: Optional[str] = None  # profile name
    task_instruction: Optional[str] = None  # truncated, for listings
    provider: Optional[str] = None
    routing_tier: Optional[str] = None
    routing_reason: Optional[str] = None


class EngagementEntry(BaseModel):
    """A single message entry in the engagement file."""

    type: str = "message"
    role: str
    content: str
    call_id: Optional[str] = None
    raw_blocks: Optional[List[Any]] = None
    # Compaction provenance carried through from `Message`. Without these on the
    # entry the marks are dropped on persist, and a resumed engagement would
    # re-summarize summaries it had already compacted. Optional with None
    # defaults, so pre-existing files load unchanged.
    summary_level: Optional[int] = None
    artifact_ref: Optional[int] = None
    partial: bool = False
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
    verbosity: str
    approval_mode: str
    summary: str
    message_count: int
    kind: str = "conversation"
    parent_engagement_id: Optional[str] = None
    agent_type: Optional[str] = None
    task_instruction: Optional[str] = None
    provider: Optional[str] = None
    routing_tier: Optional[str] = None
    routing_reason: Optional[str] = None


class Engangement(BaseModel):
    metadata: EngagementMetadata
    messages: List[Message]


class EngagementStore:
    """Manages engagement persistence using JSONL files."""

    def __init__(self, store_dir: Optional[Path] = None):
        self.store_dir = store_dir or DEFAULT_STORE_DIR
        self._ensure_dir()
        self._engagement_id: Optional[str] = None
        self._message_count: int = 0
        self._routing_tier: Optional[str] = None
        self._usage_gap: bool = False

    @property
    def engagement_id(self) -> Optional[str]:
        return self._engagement_id

    def detach(self) -> None:
        """Stop appending to the selected conversation without deleting it."""
        self._engagement_id = None
        self._message_count = 0

    def _ensure_dir(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _engagement_path(self, engagement_id: str) -> Path:
        return self.store_dir / f"{engagement_id}.jsonl"

    def start_engagement(
        self,
        mode: str,
        model: str,
        verbosity: str,
        approval_mode: str,
        kind: str = "conversation",
        parent_engagement_id: Optional[str] = None,
        parent_call_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        task_instruction: Optional[str] = None,
        provider: Optional[str] = None,
        routing_tier: Optional[str] = None,
        routing_reason: Optional[str] = None,
    ) -> str:
        """Start a new engagement. Returns the engagement ID.

        Pass `kind="agent"` plus the parent/agent fields to record an agent
        sub-run. Conversation engagements leave them at their defaults.
        """
        self._message_count = 0
        self._usage_gap = False
        now_dt = datetime.now(timezone.utc)
        date_str = now_dt.strftime("%Y-%m-%d")
        self._engagement_id = f"{date_str}-{uuid.uuid4().hex[:8]}"
        now = now_dt.isoformat()

        metadata = EngagementMetadata(
            id=self._engagement_id,
            created_at=now,
            updated_at=now,
            mode=mode,
            model=model,
            verbosity=verbosity,
            approval_mode=approval_mode,
            kind=kind,
            parent_engagement_id=parent_engagement_id,
            parent_call_id=parent_call_id,
            agent_type=agent_type,
            task_instruction=task_instruction,
            provider=provider,
            routing_tier=routing_tier,
            routing_reason=routing_reason,
        )

        path = self._engagement_path(self._engagement_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "metadata", **metadata.model_dump()}) + "\n")

        self._routing_tier = routing_tier
        self._append_usage_event(
            {"type": "measurement_start", "complete_history": True}
        )

        logger.info(f"Started engagement: {self._engagement_id} (kind={kind})")
        return self._engagement_id

    def _append_usage_event(self, event: dict) -> None:
        """Append measurements outside the compactable transcript.

        A legacy engagement's first measurement explicitly marks earlier usage
        as unknown. Persistence errors must not interrupt the conversation.
        """
        if not self._engagement_id:
            return
        path = self.store_dir / "usage" / f"{self._engagement_id}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            needs_start = not path.exists() and event["type"] != "measurement_start"
            common = {
                "version": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            with path.open("a", encoding="utf-8") as handle:
                if needs_start:
                    handle.write(
                        json.dumps(
                            {
                                **common,
                                "type": "measurement_start",
                                "complete_history": False,
                            }
                        )
                        + "\n"
                    )
                if self._usage_gap:
                    handle.write(
                        json.dumps({**common, "type": "measurement_gap"}) + "\n"
                    )
                handle.write(json.dumps({**common, **event}) + "\n")
            self._usage_gap = False
        except OSError as exc:
            self._usage_gap = True
            logger.warning(f"Could not persist usage for {self._engagement_id}: {exc}")

    def record_model_call(
        self,
        *,
        provider: str,
        model: str,
        usage: Optional[TokenUsage],
        elapsed_seconds: float,
        status: str,
        purpose: str = "completion",
    ) -> None:
        """Record one provider call, including calls with unavailable usage."""
        self._append_usage_event(
            {
                "type": "model_call",
                "provider": provider,
                "model": model,
                "tier": self._routing_tier,
                "usage": usage.model_dump() if usage is not None else None,
                "elapsed_seconds": elapsed_seconds,
                "status": status,
                "purpose": purpose,
            }
        )

    def record_run_outcome(
        self,
        *,
        status: str,
        elapsed_seconds: float,
        iterations: int,
        report_status: Optional[str] = None,
    ) -> None:
        """Record an agent result or one main-conversation turn's outcome."""
        self._append_usage_event(
            {
                "type": "run_outcome",
                "status": status,
                "report_status": report_status,
                "elapsed_seconds": elapsed_seconds,
                "iterations": iterations,
            }
        )

    def override_engagement_messages(self, update_messages: List[Message]) -> None:
        """Replace all stored messages in the current engagement with the given list.

        Preserves the metadata header; rewrites all message lines. Used after
        context compaction when the in-memory history has been collapsed into
        a summary and should be re-persisted.
        """
        if not self._engagement_id:
            return

        path = self._engagement_path(self._engagement_id)
        if not path.exists():
            logger.warning(f"Engagement file not found: {self._engagement_id}")
            return

        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return

        now = datetime.now(timezone.utc).isoformat()
        metadata = json.loads(lines[0])
        metadata["updated_at"] = now

        new_lines = [json.dumps(metadata)]
        for message in update_messages:
            entry = EngagementEntry(
                role=message.role,
                content=message.content,
                call_id=message.call_id,
                raw_blocks=_serialize_raw_blocks(message.raw_blocks),
                summary_level=message.summary_level,
                artifact_ref=message.artifact_ref,
                partial=message.partial,
                timestamp=now,
            )
            new_lines.append(json.dumps(entry.model_dump()))

        # Prepare the entire replacement before touching the saved conversation.
        # A failed compaction write must not truncate the working engagement.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write("\n".join(new_lines) + "\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._message_count = len(update_messages)
        logger.info(
            f"Overrode engagement {self._engagement_id} with {self._message_count} messages"
        )

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
            summary_level=message.summary_level,
            artifact_ref=message.artifact_ref,
            partial=message.partial,
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
        verbosity: Optional[str] = None,
        approval_mode: Optional[str] = None,
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
        if verbosity is not None:
            metadata["verbosity"] = verbosity
        if approval_mode is not None:
            metadata["approval_mode"] = approval_mode

        lines[0] = json.dumps(metadata)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def update_summary(self, summary: str) -> None:
        """Update the engagement summary in metadata."""
        self._update_metadata(summary=summary)

    def update_verbosity(self, verbosity: str) -> None:
        """Update the verbosity in metadata."""
        self._update_metadata(verbosity=verbosity)

    def update_approval_mode(self, approval_mode: str) -> None:
        """Update the approval mode in metadata."""
        self._update_metadata(approval_mode=approval_mode)

    def load_engagement(self, engagement_id: str) -> Engangement | None:
        """Load messages from an engagement file."""
        path = self._engagement_path(engagement_id)
        if not path.exists():
            logger.warning(f"Engagement file not found: {engagement_id}")
            return None

        messages: List[Message] = []
        lines = path.read_text(encoding="utf-8").splitlines()

        metadata = self._load_metadata(lines[0])

        if not metadata:
            raise ValueError(
                "Engagement metadata is malformed and unable to be loaded."
            )

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
                        raw_blocks=_sanitize_raw_blocks(entry.get("raw_blocks")),
                        summary_level=entry.get("summary_level"),
                        artifact_ref=entry.get("artifact_ref"),
                        partial=entry.get("partial", False),
                    )
                )

        self._engagement_id = engagement_id
        self._routing_tier = metadata.routing_tier
        self._usage_gap = False
        message_count = len(messages)
        self._message_count = message_count
        logger.info(f"Loaded engagement {engagement_id} with {message_count} messages")

        return Engangement(
            metadata=metadata,
            messages=messages,
        )

    def read_metadata(self, engagement_id: str) -> Optional[EngagementMetadata]:
        """Read one engagement's metadata header without loading its messages."""
        path = self._engagement_path(engagement_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
        except OSError as e:
            logger.warning(f"Could not read engagement {engagement_id}: {e}")
            return None
        if not first_line:
            return None
        try:
            return self._load_metadata(first_line)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Malformed metadata for engagement {engagement_id}: {e}")
            return None

    def parent_engagement_id_of(self, engagement_id: str) -> Optional[str]:
        """The engagement that spawned this one, or None for a root.

        Used to resolve which knowledge base an engagement belongs to: agents
        share their master's, so the id is walked up to the root of the tree.
        """
        metadata = self.read_metadata(engagement_id)
        return metadata.parent_engagement_id if metadata else None

    def get_latest_engagement_id(self) -> Optional[str]:
        """Get the most recently updated conversation ID.

        Conversations only: agent sub-runs share this directory, so an
        unfiltered lookup hands `--continue` a run it cannot restore.
        """
        engagements = self.list_engagements(limit=1, kind="conversation")
        if not engagements:
            return None
        return engagements[0].id

    def count_by_kind(self, kind: str) -> int:
        """Count all engagement files whose metadata `kind` matches.

        Reads only the first line of each file (the metadata header), so this
        is cheaper than `list_engagements` which also line-counts the body.
        """
        total = 0
        for path in self.store_dir.glob("*.jsonl"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                metadata = json.loads(first_line)
                if metadata.get("type") != "metadata":
                    continue
                if metadata.get("kind", "conversation") == kind:
                    total += 1
            except (json.JSONDecodeError, OSError):
                continue
        return total

    def list_engagements(
        self,
        limit: int,
        kind: Optional[str] = None,
        *,
        query: str = "",
        offset: int = 0,
    ) -> List[EngagementInfo]:
        """List saved engagements, most recent first.

        `kind` filters before the limit is applied — without it a directory of
        agent sub-runs can push every conversation out of the result.
        """
        engagements: List[EngagementInfo] = []
        search = query.strip().casefold()

        for path in self.store_dir.glob("*.jsonl"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue

                metadata = json.loads(first_line)
                if metadata.get("type") != "metadata":
                    continue
                if kind is not None and metadata.get("kind", "conversation") != kind:
                    continue
                verbosity, approval_mode = _metadata_settings(metadata)

                # One scan supplies the count and a useful fallback title for
                # sessions whose metadata summary has not been set.
                line_count = 0
                summary = metadata.get("summary", "")
                with open(path, "r", encoding="utf-8") as f:
                    next(f, None)
                    for line in f:
                        line_count += 1
                        if not summary:
                            try:
                                entry = json.loads(line)
                                if entry.get("role") == "user":
                                    summary = " ".join(
                                        entry.get("content", "").split()
                                    )[:240]
                            except (ValueError, AttributeError, TypeError):
                                pass
                if (
                    search
                    and search
                    not in " ".join(
                        str(value)
                        for value in (
                            metadata.get("id", ""),
                            summary,
                            metadata.get("mode", ""),
                            metadata.get("model", ""),
                        )
                    ).casefold()
                ):
                    continue

                engagements.append(
                    EngagementInfo(
                        id=metadata["id"],
                        created_at=metadata["created_at"],
                        updated_at=metadata["updated_at"],
                        mode=metadata.get("mode", "unknown"),
                        model=metadata.get("model", "unknown"),
                        verbosity=verbosity,
                        approval_mode=approval_mode,
                        summary=summary,
                        message_count=max(0, line_count),
                        kind=metadata.get("kind", "conversation"),
                        parent_engagement_id=metadata.get("parent_engagement_id"),
                        agent_type=metadata.get("agent_type"),
                        task_instruction=metadata.get("task_instruction"),
                        provider=metadata.get("provider"),
                        routing_tier=metadata.get("routing_tier"),
                        routing_reason=metadata.get("routing_reason"),
                    )
                )
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                OSError,
                ValidationError,
            ) as e:
                logger.warning(f"Skipping malformed engagement file {path.name}: {e}")
                continue

        engagements.sort(key=lambda s: s.updated_at, reverse=True)
        return engagements[offset : offset + limit]

    def _load_metadata(self, firstLine: str) -> EngagementMetadata | None:
        try:
            dictMetadata = json.loads(firstLine)

            if dictMetadata["type"] != "metadata":
                return None

            verbosity, approval_mode = _metadata_settings(dictMetadata)
            return EngagementMetadata(
                id=dictMetadata["id"],
                created_at=dictMetadata["created_at"],
                updated_at=dictMetadata["updated_at"],
                mode=dictMetadata["mode"],
                model=dictMetadata["model"],
                verbosity=verbosity,
                approval_mode=approval_mode,
                summary=dictMetadata.get("summary", ""),
                kind=dictMetadata.get("kind", "conversation"),
                parent_engagement_id=dictMetadata.get("parent_engagement_id"),
                parent_call_id=dictMetadata.get("parent_call_id"),
                agent_type=dictMetadata.get("agent_type"),
                task_instruction=dictMetadata.get("task_instruction"),
                provider=dictMetadata.get("provider"),
                routing_tier=dictMetadata.get("routing_tier"),
                routing_reason=dictMetadata.get("routing_reason"),
            )
        except Exception as e:
            logger.error(
                f"Exception occurred when trying to load EngagementMetadata: {e}"
            )
            return None

    def delete_engagement(self, engagement_id: str) -> bool:
        """Delete an engagement file and its share of the knowledge base."""
        path = self._engagement_path(engagement_id)
        if not path.exists():
            return False

        # Read the parent link before unlinking — it decides whether this
        # engagement owns the knowledge base file or merely has rows in one.
        parent_id = self.parent_engagement_id_of(engagement_id)
        path.unlink()
        (self.store_dir / "usage" / f"{engagement_id}.jsonl").unlink(missing_ok=True)
        self._delete_knowledge_base(engagement_id, parent_id)

        logger.info(f"Deleted engagement: {engagement_id}")
        if self._engagement_id == engagement_id:
            self._engagement_id = None
            self._message_count = 0
        return True

    def _delete_knowledge_base(
        self, engagement_id: str, parent_id: Optional[str]
    ) -> None:
        """Drop the deleted engagement's artifacts.

        A root owns its `.kb.sqlite` outright, so the file goes. An agent
        sub-run only has rows inside its master's file — deleting that file
        would take the master's whole engagement with it, so just its rows go.
        """
        try:
            if parent_id is None:
                delete_kb_for_root(self.store_dir, engagement_id)
                return
            # Walk from the parent, not from `engagement_id`: its own file is
            # already unlinked, so a walk starting there would find no parent
            # link and wrongly conclude it was a root.
            root_id = resolve_root_engagement_id(
                parent_id, self.parent_engagement_id_of
            )
            if not kb_path_for(self.store_dir, root_id).exists():
                return
            kb = EngagementKnowledgeBase(root_id, self.store_dir)
            try:
                kb.delete_engagement_rows(engagement_id)
                kb.delete_engagement_ledger_rows(engagement_id)
            finally:
                kb.close()
        except Exception as e:
            logger.warning(f"Could not clean knowledge base for {engagement_id}: {e}")
