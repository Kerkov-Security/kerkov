"""Read-only view of the files an engagement generated in its run workspace."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, TypeGuard

if TYPE_CHECKING:
    from kerkov.web.runtime.session import WebKerkovSession

MAX_REQUEST_ID_CHARS = 128
MAX_PATH_CHARS = 4096
MAX_FILES = 1000
MAX_READ_BYTES = 512 * 1024

# Extension → highlighter hint the browser understands. Kept small and
# deterministic; an unknown extension renders as plain monospace text.
LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyw": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".php": "php",
    ".pl": "perl",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".xml": "html",
    ".css": "css",
    ".md": "markdown",
    ".txt": "",
    ".log": "",
}

# Extension-less scripts are common; match a few by basename.
LANGUAGE_BY_NAME = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
}


class ScriptsRequestError(ValueError):
    """A literal, safe-to-display request failure."""


class WebScripts:
    """Validate requests and project the run workspace's files, read-only."""

    def __init__(self, session: WebKerkovSession) -> None:
        self.session = session
        self.context_id = uuid.uuid4().hex
        self.engagement_id: Optional[str] = None
        self._requests: set[asyncio.Future[None]] = set()

    def reset(self) -> None:
        """Retire old response tokens immediately, before the workspace changes."""
        self.context_id = uuid.uuid4().hex
        self.engagement_id = None
        self._snapshot("", [], available=False)

    async def settle(self) -> None:
        """Finish in-flight reads before the workspace path is cleared."""
        if self._requests:
            await asyncio.gather(*tuple(self._requests), return_exceptions=True)

    async def handle(
        self, content: dict[str, Any], *, message_pending: bool = False
    ) -> None:
        if self.session._closed:
            return
        request_id = content.get("request_id")
        operation = content.get("type", "").removeprefix("scripts.")
        if not self._valid_token(request_id):
            self._error("", operation, "Invalid scripts request ID.")
            return
        finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._requests.add(finished)
        context_id = self.context_id
        try:
            if operation == "list":
                await self._list(request_id, context_id)
            elif operation == "read":
                if content.get("context_id") != context_id:
                    raise ScriptsRequestError(
                        "These scripts have changed. Refresh and retry."
                    )
                await self._read(content, request_id, context_id)
            else:
                raise ScriptsRequestError("Unsupported scripts operation.")
        except ScriptsRequestError as exc:
            if self._current(context_id):
                self._error(request_id, operation, str(exc))
        except Exception:
            # Workspace files may hold harvested secrets and raw tool output.
            if self._current(context_id):
                self._error(
                    request_id,
                    operation,
                    "The workspace could not be read. Refresh and try again.",
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

    def _current(self, context_id: str) -> bool:
        return context_id == self.context_id and not self.session._closed

    @staticmethod
    def _language(name: str) -> str:
        lowered = name.lower()
        if lowered in LANGUAGE_BY_NAME:
            return LANGUAGE_BY_NAME[lowered]
        return LANGUAGE_BY_EXTENSION.get(Path(lowered).suffix, "")

    def _snapshot(
        self,
        request_id: str,
        files: list[dict[str, Any]],
        *,
        available: bool,
        detail: str = "",
        truncated: bool = False,
    ) -> None:
        self.session.bus.emit(
            "scripts.snapshot",
            {
                "request_id": request_id,
                "context_id": self.context_id,
                "engagement_id": self.engagement_id,
                "available": available,
                "detail": detail,
                "truncated": truncated,
                "files": files,
            },
        )

    def _scan(self, root: Path) -> tuple[list[dict[str, Any]], bool]:
        resolved_root = root.resolve()
        files: list[dict[str, Any]] = []
        # Walk one past the cap so a workspace with exactly MAX_FILES files is
        # not reported as truncated; the extra row is sliced off below.
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                try:
                    # relative_to raises for anything symlinked outside the root.
                    relative = full.resolve().relative_to(resolved_root).as_posix()
                    if not full.is_file():
                        continue
                    stat = full.stat()
                except (OSError, ValueError):
                    continue
                files.append(
                    {
                        "path": relative,
                        "name": filename,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "language": self._language(filename),
                    }
                )
                if len(files) > MAX_FILES:
                    files.sort(key=lambda row: row["path"])
                    return files[:MAX_FILES], True
        files.sort(key=lambda row: row["path"])
        return files, False

    async def _list(self, request_id: str, context_id: str) -> None:
        root = self.session.workspace_path
        if root is None:
            if self._current(context_id):
                self._snapshot(
                    request_id,
                    [],
                    available=False,
                    detail="Scripts appear here once the engagement starts.",
                )
            return
        files, truncated = await asyncio.to_thread(self._scan, root)
        if not self._current(context_id):
            return
        detail = "" if files else "No scripts have been generated yet."
        self._snapshot(
            request_id, files, available=True, detail=detail, truncated=truncated
        )

    def _resolve(self, root: Path, requested: str) -> Path:
        if not isinstance(requested, str) or not requested:
            raise ScriptsRequestError("Choose a script to view.")
        if len(requested) > MAX_PATH_CHARS or "\x00" in requested:
            raise ScriptsRequestError("Invalid script path.")
        resolved_root = root.resolve()
        target = (resolved_root / requested).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError:
            raise ScriptsRequestError("That script is outside the workspace.") from None
        if not target.is_file():
            raise ScriptsRequestError("Script no longer exists. Refresh and retry.")
        return target

    def _load(self, root: Path, requested: Any) -> dict[str, Any]:
        # Resolve, confirm and read in one thread hop so no filesystem call
        # touches the event loop and nothing swaps the file between the
        # is_file() check and the open().
        target = self._resolve(root, requested)
        size = target.stat().st_size
        with open(target, "rb") as handle:
            raw = handle.read(MAX_READ_BYTES)
        binary = b"\x00" in raw
        text = "" if binary else raw.decode("utf-8", errors="replace")
        relative = target.resolve().relative_to(root.resolve()).as_posix()
        return {
            "path": relative,
            "name": target.name,
            "language": self._language(target.name),
            "content": text,
            "binary": binary,
            "truncated": size > len(raw),
            "size": size,
        }

    async def _read(
        self, content: dict[str, Any], request_id: str, context_id: str
    ) -> None:
        root = self.session.workspace_path
        if root is None:
            raise ScriptsRequestError("Script no longer exists. Refresh and retry.")
        loaded = await asyncio.to_thread(self._load, root, content.get("path"))
        if not self._current(context_id):
            return
        self.session.bus.emit(
            "scripts.content",
            {
                "request_id": request_id,
                "context_id": context_id,
                **loaded,
            },
        )

    def _error(self, request_id: str, operation: str, detail: str) -> None:
        self.session.bus.emit(
            "scripts.error",
            {
                "request_id": request_id,
                "context_id": self.context_id,
                "operation": operation,
                "detail": detail,
            },
        )
