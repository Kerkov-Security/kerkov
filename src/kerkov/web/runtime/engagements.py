"""Saved conversation navigation for the local web console."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import weakref
from typing import TYPE_CHECKING, Any

from kerkov.cli.approval_mode_manager import ApprovalMode
from kerkov.cli.approval_rules import ApprovalRulesEngine
from kerkov.cli.engagement_restore import restored_setting
from kerkov.cli.engagement_store import EngagementStore, Engangement
from kerkov.cli.modes import ModeType
from kerkov.cli.verbosity_manager import Verbosity
from kerkov.cli.verbosity_prompts import apply_verbosity_to_system_prompt
from kerkov.context.token_tracker import TokenTracker
from kerkov.core.knowledge_base.engagement_kb import kb_path_for, open_knowledge_base
from kerkov.core.knowledge_base.findings_ledger import FindingsLedger
from kerkov.models.basic_models import Message
from kerkov.web.runtime.markdown_html import render_markdown
from kerkov.web.runtime.provider_history import (
    HistoryPreparationContext,
    prepare_history,
)
from kerkov.web.runtime.providers import (
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    REQUEST_ID_PATTERN,
    ProviderSelectionError,
)

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession

logger = logging.getLogger(__name__)

ENGAGEMENT_REQUESTS = {
    "engagement.list",
    "engagement.resume",
    "engagement.new",
    "engagement.history",
}
ENGAGEMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
PAGE_SIZE = 20
HISTORY_PAGE_SIZE = 50
HISTORY_PAGE_CHARS = 64_000
MESSAGE_PREVIEW_CHARS = 8_000
MAX_TRANSCRIPT_BYTES = 20 * 1024 * 1024


class EngagementRequestError(ValueError):
    """A safe user-facing navigation failure."""


class WebEngagements:
    """List saved roots and replace a session only after validating a candidate."""

    _owners: weakref.WeakValueDictionary[str, WebKerkovSession] = (
        weakref.WeakValueDictionary()
    )

    def __init__(self, session: WebKerkovSession) -> None:
        self.session = session
        self.context_id = uuid.uuid4().hex
        self._claimed: str | None = None
        self._history_messages: list[Message] = []

    def _is_closed(self) -> bool:
        """Read lifecycle state afresh after awaited disk and cleanup operations."""
        return self.session._closed

    def _key(self, engagement_id: str) -> str:
        return str(self.session.engagement_store.store_dir.resolve() / engagement_id)

    def claim(self, engagement_id: str) -> None:
        key = self._key(engagement_id)
        owner = self._owners.get(key)
        if owner is not None and owner is not self.session and not owner._closed:
            raise EngagementRequestError(
                "This engagement is open in another browser session."
            )
        self._owners[key] = self.session
        self._claimed = key

    def release(self) -> None:
        if self._claimed and self._owners.get(self._claimed) is self.session:
            del self._owners[self._claimed]
        self._claimed = None

    def reset(self) -> None:
        self.release()
        self._history_messages = []
        self.context_id = uuid.uuid4().hex

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if not self.session._closed:
            self.session.bus.emit(event, {**payload, "context_id": self.context_id})

    async def handle(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        if self.session._closed:
            return
        request_id = content.get("request_id")
        operation = str(content.get("type", "")).removeprefix("engagement.")
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(
            request_id
        ):
            self._emit(
                "engagement.error",
                {
                    "request_id": "",
                    "operation": operation,
                    "detail": "Invalid engagement request ID.",
                },
            )
            return
        try:
            if operation == "list":
                await self._list(content, request_id)
                return
            if content.get("context_id") != self.context_id:
                raise EngagementRequestError(
                    "The session changed. Refresh engagements and retry."
                )
            if operation == "history":
                before = content.get("before")
                if type(before) is not int or not 0 < before <= len(
                    self._history_messages
                ):
                    raise EngagementRequestError("Invalid conversation page.")
                self._emit(
                    "engagement.history",
                    {"request_id": request_id, **self._history(before)},
                )
                return
            if operation not in {"resume", "new"}:
                raise EngagementRequestError("Unsupported engagement operation.")
            session = self.session
            if (
                message_pending
                or session._lock.locked()
                or session.agent_supervisor.active()
            ):
                raise EngagementRequestError(
                    "Wait for the current message, provider change, and agents to finish."
                )
            async with session._lock:
                if operation == "new":
                    await session._handle_web_command("/clear")
                    self._emit(
                        "engagement.started",
                        {"request_id": request_id, "engagement_id": None},
                    )
                else:
                    await self._resume(content.get("engagement_id"), request_id)
        except EngagementRequestError as exc:
            self._emit(
                "engagement.error",
                {"request_id": request_id, "operation": operation, "detail": str(exc)},
            )
        except Exception:
            logger.exception("Saved engagement request failed")
            self._emit(
                "engagement.error",
                {
                    "request_id": request_id,
                    "operation": operation,
                    "detail": "The saved engagement could not be opened. Refresh and retry.",
                },
            )

    async def _list(self, content: dict[str, Any], request_id: str) -> None:
        query = content.get("query", "")
        offset = content.get("offset", 0)
        if not isinstance(query, str) or len(query) > 200:
            raise EngagementRequestError("Search must be at most 200 characters.")
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise EngagementRequestError("Invalid engagement page.")
        rows = await asyncio.to_thread(
            self.session.engagement_store.list_engagements,
            PAGE_SIZE + 1,
            kind="conversation",
            query=query,
            offset=offset,
        )
        active = self.session.inventory.engagement_id
        self._emit(
            "engagement.list",
            {
                "request_id": request_id,
                "query": query,
                "offset": offset,
                "next_offset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None,
                "active_id": active,
                "engagements": [
                    {
                        "id": row.id,
                        "summary": row.summary[:240],
                        "mode": row.mode[:40],
                        "model": row.model[:256],
                        "updated_at": row.updated_at[:64],
                        "message_count": row.message_count,
                        "approval_mode": row.approval_mode,
                        "in_use": self._owners.get(self._key(row.id))
                        not in (None, self.session),
                    }
                    for row in rows[:PAGE_SIZE]
                ],
            },
        )

    def _load(self, engagement_id: Any) -> tuple[EngagementStore, Engangement]:
        if not isinstance(engagement_id, str) or not ENGAGEMENT_ID_PATTERN.fullmatch(
            engagement_id
        ):
            raise EngagementRequestError("Invalid engagement ID.")
        store = EngagementStore(self.session.engagement_store.store_dir)
        path = store.store_dir / f"{engagement_id}.jsonl"
        if path.is_symlink() or path.resolve().parent != store.store_dir.resolve():
            raise EngagementRequestError("Invalid engagement path.")
        if not path.is_file():
            raise EngagementRequestError(
                "This engagement no longer exists. Refresh the list."
            )
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise EngagementRequestError(
                "This engagement is too large for the web console. Open it in the CLI."
            )
        engagement = store.load_engagement(engagement_id)
        if engagement is None or engagement.metadata.id != engagement_id:
            raise EngagementRequestError("The saved engagement has invalid metadata.")
        if (
            engagement.metadata.kind != "conversation"
            or engagement.metadata.parent_engagement_id
        ):
            raise EngagementRequestError(
                "Only saved conversations can be resumed here."
            )
        if not engagement.messages or engagement.messages[0].role != "system":
            raise EngagementRequestError(
                "The saved conversation has no system context."
            )
        return store, engagement

    async def _resume(self, engagement_id: Any, request_id: str) -> None:
        session = self.session
        if engagement_id == session.inventory.engagement_id:
            raise EngagementRequestError("This engagement is already open.")
        store, engagement = await asyncio.to_thread(self._load, engagement_id)
        metadata = engagement.metadata
        try:
            mode = restored_setting(
                ModeType, metadata.mode, metadata.id, "mode", by_name=True
            )
            verbosity = restored_setting(
                Verbosity, metadata.verbosity, metadata.id, "verbosity"
            )
            approval = restored_setting(
                ApprovalMode, metadata.approval_mode, metadata.id, "approval mode"
            )
        except Exception:
            raise EngagementRequestError(
                "This engagement records settings this version cannot restore."
            ) from None
        if (
            approval == ApprovalMode.YOLO
            and not session.approval_mode_manager.allow_autonomous
        ):
            raise EngagementRequestError(
                "This engagement requires ALLOW_AUTONOMOUS=true to restore its approval mode."
            )
        # Prepare disk state before retiring the current conversation. No completion
        # call or transcript rewrite is required just to open a saved session.
        workspace = session.workspace_manager.path_for_engagement(metadata.id)
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        try:
            workspace_rules = await asyncio.to_thread(
                ApprovalRulesEngine.from_file, workspace / "approvals.yaml"
            )
        except ValueError as exc:
            raise EngagementRequestError(
                "This engagement has invalid workspace approvals.yaml rules. "
                "Fix that file before resuming; your current conversation is unchanged."
            ) from exc
        if self._is_closed():
            return
        key = self._key(metadata.id)
        owner = self._owners.get(key)
        if owner is not None and owner is not session and not owner._closed:
            raise EngagementRequestError(
                "This engagement is open in another browser session."
            )
        # Reserve the candidate across the await that settles old observers.
        self._owners[key] = session
        try:
            apply_verbosity_to_system_prompt(engagement.messages, verbosity)
            try:
                await asyncio.wait_for(
                    self._validate_history(engagement),
                    timeout=PROVIDER_REQUEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise EngagementRequestError(
                    "Checking the saved conversation against the current model timed "
                    "out. Your current conversation is unchanged. Retry or choose "
                    "another model."
                ) from None
            if self._is_closed():
                return
            await session._retire_engagement()
            if self._is_closed():
                return
            session.engagement_store = store
            self.claim(metadata.id)
            # The supervisor and delegate tool must capture the newly loaded store.
            await session._reset_agents()
            if self._is_closed():
                return
            session.approvals.cancel_all()
            session.approval_strategy.clear_session_rules()
            session.mode_manager.switch_mode(mode)
            session.verbosity_manager.set(verbosity)
            session.approval_mode_manager.set(approval)
            session.conversation_history = engagement.messages
            self._history_messages = list(session.conversation_history)
            session.token_tracker.reset()
            session.compactor = None
            session.compaction_warning_shown = False
            session.workspace_path = workspace
            session.approval_strategy.set_workspace(
                workspace, rules=workspace_rules.file_rules
            )
            session._open_knowledge_base(metadata.id)
            session.inventory.engagement_id = metadata.id
            self._emit(
                "engagement.resumed",
                {
                    "request_id": request_id,
                    "engagement_id": metadata.id,
                    "mode": mode.value,
                    "mode_name": session.mode_manager.current_mode.name,
                    "approval_mode": approval.value,
                    "verbosity": verbosity.value,
                    "saved_model": metadata.model,
                    **self._history(),
                },
            )
        finally:
            if self._claimed != key and self._owners.get(key) is session:
                del self._owners[key]

    async def _validate_history(self, engagement: Engangement) -> None:
        """Reject history that cannot fit before retiring the current conversation."""
        session = self.session
        provider = session.provider
        if provider is None:
            # Selecting a provider later validates the installed history through
            # the same preparation helper before activating the selected model.
            return
        try:
            max_context, max_output = await session.providers._candidate_limits(
                provider
            )
            if self._is_closed():
                return
            knowledge_base = (
                open_knowledge_base(
                    engagement.metadata.id, session.engagement_store.store_dir
                )
                if session.config.kb_enabled
                and kb_path_for(
                    session.engagement_store.store_dir, engagement.metadata.id
                ).is_file()
                else None
            )
            try:
                candidate = HistoryPreparationContext(
                    conversation_history=engagement.messages,
                    tools=session.tools,
                    findings_ledger=(
                        FindingsLedger(knowledge_base)
                        if knowledge_base is not None
                        else None
                    ),
                    token_tracker=TokenTracker(),
                    config=session.config,
                )
                await prepare_history(
                    candidate, max_context, max_output, use_compaction_threshold=False
                )
            finally:
                if knowledge_base is not None:
                    # A cancelled ledger read may still own the SQLite lock in
                    # its worker; never wait for that lock on the ASGI loop.
                    await asyncio.to_thread(knowledge_base.close)
        except ProviderSelectionError as exc:
            raise EngagementRequestError(str(exc)) from None

    def _history(self, before: int | None = None) -> dict[str, Any]:
        history = self._history_messages
        cursor = len(history) if before is None else before
        rows: list[dict[str, Any]] = []
        remaining = HISTORY_PAGE_CHARS
        while cursor > 0 and len(rows) < HISTORY_PAGE_SIZE:
            message = history[cursor - 1]
            if message.role not in {"user", "assistant", "tool"} or not message.content:
                cursor -= 1
                continue
            text = message.content[:MESSAGE_PREVIEW_CHARS]
            row: dict[str, Any] = {
                "role": message.role,
                "content": text,
                "truncated": len(text) < len(message.content),
                "partial": message.partial,
            }
            if message.role == "assistant":
                row["html"] = render_markdown(text)
            size = len(text) + len(row.get("html", ""))
            if size > remaining:
                break
            cursor -= 1
            remaining -= size
            rows.append(row)
        return {"messages": list(reversed(rows)), "before": cursor or None}
