"""Assets and targets adapter for the browser-owned engagement."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Optional, TypeGuard

from pydantic import ValidationError

from kerkov.core.knowledge_base.connections import project_resource_graph
from kerkov.core.knowledge_base.models import (
    RESOURCE_SECTIONS,
    LedgerEntry,
    LedgerSection,
    Resource,
)

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession

MAX_REQUEST_ID_CHARS = 128
MAX_RESOURCE_VALUE_CHARS = 8192
MAX_RESOURCE_NAME_CHARS = 256
MAX_EVIDENCE_CHARS = 32_000


class InventoryRequestError(ValueError):
    """A literal, safe-to-display request failure."""


class WebInventory:
    """Validate requests and project only resource rows from the shared ledger."""

    def __init__(self, session: WebKerkovSession) -> None:
        self.session = session
        self.context_id = uuid.uuid4().hex
        self.engagement_id: Optional[str] = None
        self._requests: set[asyncio.Future[None]] = set()

    def reset(self) -> None:
        """Retire old response tokens immediately, before agents settle."""
        self.context_id = uuid.uuid4().hex
        self.engagement_id = None
        self._snapshot("", [], available=self.session.config.kb_enabled)

    async def settle(self) -> None:
        """Finish old reads before their detached knowledge base is closed."""
        if self._requests:
            await asyncio.gather(*tuple(self._requests), return_exceptions=True)

    async def handle(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        if self.session._closed:
            return
        request_id = content.get("request_id")
        operation = content.get("type", "").removeprefix("inventory.")
        if not self._valid_token(request_id):
            self._error("", operation, "Invalid inventory request ID.")
            return
        finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._requests.add(finished)
        context_id = self.context_id
        try:
            if operation == "list":
                await self._list(request_id, context_id)
                return
            if content.get("context_id") != context_id:
                raise InventoryRequestError(
                    "This inventory has changed. Refresh and retry."
                )
            if operation == "save":
                await self._save(content, request_id, message_pending=message_pending)
            elif operation == "profile":
                await self._profile(content, request_id, context_id)
            elif operation == "profile_evidence":
                await self._profile_evidence(content, request_id, context_id)
            elif operation == "evidence":
                await self._evidence(content, request_id, context_id)
            else:
                raise InventoryRequestError("Unsupported inventory operation.")
        except InventoryRequestError as exc:
            if self._current(context_id):
                self._error(request_id, operation, str(exc))
        except Exception:
            # Resource values, database paths, and evidence may contain secrets.
            if self._current(context_id):
                self._error(
                    request_id,
                    operation,
                    "Inventory could not be accessed. Refresh and try again.",
                )
        finally:
            if not finished.done():
                finished.set_result(None)
            self._requests.discard(finished)

    @staticmethod
    def _valid_token(value: Any) -> TypeGuard[str]:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= MAX_REQUEST_ID_CHARS
            and all(ord(char) >= 32 and ord(char) != 127 for char in value)
        )

    @staticmethod
    def _entry_id(value: Any, *, optional: bool = False) -> Optional[int]:
        if optional and value is None:
            return None
        if type(value) is not int or not 0 < value < 2**63:
            raise InventoryRequestError("Invalid resource ID.")
        return value

    def _current(self, context_id: str) -> bool:
        return context_id == self.context_id and not self.session._closed

    def _availability(self) -> tuple[bool, str]:
        if not self.session.config.kb_enabled:
            return False, "Assets and targets require KB_ENABLED to be enabled."
        if self.engagement_id and self.session.findings_ledger is None:
            return False, "The engagement knowledge base is unavailable."
        return True, ""

    def _snapshot(
        self,
        request_id: str,
        entries: list[LedgerEntry],
        *,
        available: bool,
        detail: str = "",
    ) -> None:
        if not available and not detail:
            _, detail = self._availability()
        self.session.bus.emit(
            "inventory.snapshot",
            {
                "request_id": request_id,
                "context_id": self.context_id,
                "engagement_id": self.engagement_id,
                "available": available,
                "detail": detail,
                "entries": [
                    {
                        "id": row.id,
                        "section": row.section.value,
                        "entry": {
                            key: row.entry[key]
                            for key in (
                                "kind",
                                "value",
                                "name",
                                "environment",
                                "tags",
                                "notes",
                            )
                            if key in row.entry
                        },
                        "artifact_ref": row.artifact_ref,
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                    }
                    for row in entries
                    if row.section in RESOURCE_SECTIONS
                ],
                "graph": project_resource_graph(entries),
            },
        )

    async def _rows(self, *, include_connections: bool = False) -> list[LedgerEntry]:
        ledger = self.session.findings_ledger
        if ledger is None:
            return []
        sections = (
            (*RESOURCE_SECTIONS, LedgerSection.CONNECTIONS, LedgerSection.CONTAINERS)
            if include_connections
            else RESOURCE_SECTIONS
        )
        # One filtered read keeps resources and connections coherent while
        # excluding unrelated ledger data (especially credentials and flags).
        return await ledger.list(sections=sections)

    async def _list(self, request_id: str, context_id: str) -> None:
        available, detail = self._availability()
        entries: list[LedgerEntry] = []
        if available:
            try:
                entries = await self._rows(include_connections=True)
            except Exception:
                available = False
                detail = "The engagement knowledge base is unavailable."
        if self._current(context_id):
            self._snapshot(request_id, entries, available=available, detail=detail)

    @staticmethod
    def _resource(content: dict[str, Any]) -> tuple[LedgerSection, Resource]:
        section = content.get("section")
        if section not in RESOURCE_SECTIONS:
            raise InventoryRequestError("Choose an asset or target role.")
        resource = content.get("resource")
        if not isinstance(resource, dict):
            raise InventoryRequestError("Resource must be an object.")
        value = resource.get("value")
        name = resource.get("name")
        if not isinstance(value, str) or len(value) > MAX_RESOURCE_VALUE_CHARS:
            raise InventoryRequestError(
                "Resource value must be at most 8,192 characters."
            )
        if name is not None and (
            not isinstance(name, str) or len(name) > MAX_RESOURCE_NAME_CHARS
        ):
            raise InventoryRequestError("Resource name must be at most 256 characters.")
        try:
            parsed = Resource.model_validate(resource)
        except ValidationError:
            raise InventoryRequestError(
                "Enter a valid domain, IP, HTTP(S) URL, or absolute repository path. "
                "Names and values cannot contain control characters. "
                "Environment: at most 128 characters; tags: at most 20 of 64 "
                "characters each; notes: at most 4,096 characters."
            ) from None
        return LedgerSection(section), parsed

    async def _save(
        self,
        content: dict[str, Any],
        request_id: str,
        *,
        message_pending: bool,
    ) -> None:
        section, resource = self._resource(content)
        entry_id = self._entry_id(content.get("entry_id"), optional=True)
        expected = content.get("expected_updated_at")
        if expected is not None and not self._valid_token(expected):
            raise InventoryRequestError("Invalid resource revision.")
        available, detail = self._availability()
        if not available:
            raise InventoryRequestError(detail)
        if message_pending or self.session._lock.locked():
            raise InventoryRequestError(
                "Wait for the current message to finish before saving."
            )
        async with self.session._lock:
            if entry_id is not None and self.engagement_id is None:
                raise InventoryRequestError(
                    "Resource no longer exists. Refresh and retry."
                )
            self.session._apply_system_prompt_if_needed()
            ledger = self.session.findings_ledger
            if ledger is None:
                raise InventoryRequestError(
                    "The engagement knowledge base is unavailable."
                )
            context_id = self.context_id
            try:
                saved_id = await ledger.save_resource(
                    section,
                    resource.model_dump(exclude_none=True),
                    entry_id=entry_id,
                    expected_updated_at=expected,
                )
            except ValueError as exc:
                safe_details = {
                    "Resource no longer exists. Refresh and retry.",
                    "Resource changed. Refresh before editing again.",
                }
                if str(exc) in safe_details:
                    raise InventoryRequestError(str(exc)) from None
                raise
            if not self._current(context_id):
                return
            self.session.bus.emit(
                "inventory.saved",
                {
                    "request_id": request_id,
                    "context_id": context_id,
                    "entry_id": saved_id,
                },
            )
            await self._list(request_id, context_id)

    async def _profile(
        self, content: dict[str, Any], request_id: str, context_id: str
    ) -> None:
        entry_id = self._entry_id(content.get("entry_id"))
        assert entry_id is not None
        available, detail = self._availability()
        if not available:
            raise InventoryRequestError(detail)
        ledger = self.session.findings_ledger
        profile = await ledger.profile(entry_id) if ledger is not None else None
        if not self._current(context_id):
            return
        if profile is None:
            raise InventoryRequestError("Resource no longer exists. Refresh and retry.")
        self.session.bus.emit(
            "inventory.profile",
            {
                "request_id": request_id,
                "context_id": context_id,
                "entry_id": entry_id,
                "profile": profile,
            },
        )

    async def _profile_evidence(
        self, content: dict[str, Any], request_id: str, context_id: str
    ) -> None:
        entry_id = self._entry_id(content.get("entry_id"))
        artifact_id = self._entry_id(content.get("artifact_id"))
        assert entry_id is not None and artifact_id is not None
        available, detail = self._availability()
        if not available:
            raise InventoryRequestError(detail)
        ledger, kb = self.session.findings_ledger, self.session.knowledge_base
        if ledger is None or kb is None:
            raise InventoryRequestError("Resource no longer exists. Refresh and retry.")

        def linked(profile: Optional[dict[str, Any]]) -> bool:
            if profile is None:
                return False
            return any(
                row["artifact_ref"] == artifact_id
                for section in ("records", "hosts", "services", "findings", "activity")
                for row in profile[section]
            ) or any(
                evidence["artifact_ref"] == artifact_id
                for edge in profile["connections"]
                for evidence in edge["evidence"]
            )

        if not linked(await ledger.profile(entry_id)):
            raise InventoryRequestError(
                "This evidence is no longer linked to the profile."
            )
        if not self._current(context_id):
            return
        artifact = await kb.get_artifact(artifact_id)
        if not self._current(context_id):
            return
        if not linked(await ledger.profile(entry_id)):
            raise InventoryRequestError(
                "This evidence is no longer linked to the profile."
            )
        if not self._current(context_id):
            return
        if artifact is None:
            raise InventoryRequestError("The linked evidence is no longer available.")
        body = artifact.body or ""
        self.session.bus.emit(
            "inventory.profile_evidence",
            {
                "request_id": request_id,
                "context_id": context_id,
                "entry_id": entry_id,
                "artifact": {
                    "artifact_id": artifact_id,
                    "title": artifact.title[:1024],
                    "source": artifact.source[:1024],
                    "body": body[:MAX_EVIDENCE_CHARS],
                    "truncated": len(body) > MAX_EVIDENCE_CHARS,
                },
            },
        )

    async def _evidence(
        self, content: dict[str, Any], request_id: str, context_id: str
    ) -> None:
        entry_id = self._entry_id(content.get("entry_id"))
        available, detail = self._availability()
        if not available:
            raise InventoryRequestError(detail)
        kb = self.session.knowledge_base
        if kb is None:
            raise InventoryRequestError("Resource no longer exists. Refresh and retry.")
        row = next((row for row in await self._rows() if row.id == entry_id), None)
        if not self._current(context_id):
            return
        if row is None:
            raise InventoryRequestError("Resource no longer exists. Refresh and retry.")
        if row.artifact_ref is None:
            raise InventoryRequestError("This resource has no linked evidence.")
        artifact = await kb.get_artifact(row.artifact_ref)
        if not self._current(context_id):
            return
        current = next((row for row in await self._rows() if row.id == entry_id), None)
        if not self._current(context_id):
            return
        if current is None or current.updated_at != row.updated_at:
            raise InventoryRequestError(
                "Resource changed. Refresh before viewing evidence again."
            )
        if artifact is None:
            raise InventoryRequestError("The linked evidence is no longer available.")
        body = artifact.body or ""
        self.session.bus.emit(
            "inventory.evidence",
            {
                "request_id": request_id,
                "context_id": context_id,
                "entry_id": entry_id,
                "artifact": {
                    "artifact_id": artifact.artifact_id,
                    "title": artifact.title[:1024],
                    "body": body[:MAX_EVIDENCE_CHARS],
                    "source": artifact.source[:1024],
                    "truncated": len(body) > MAX_EVIDENCE_CHARS,
                },
            },
        )

    def _error(self, request_id: str, operation: str, detail: str) -> None:
        self.session.bus.emit(
            "inventory.error",
            {
                "request_id": request_id,
                "context_id": self.context_id,
                "operation": operation,
                "detail": detail,
            },
        )
