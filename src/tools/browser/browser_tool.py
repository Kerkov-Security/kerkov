"""Single LLM-facing browser automation tool backed by Playwright CLI."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, Sequence, Type

from pydantic import Field

from src.core.browser.browser_session_registry import (
    BrowserSession,
    BrowserSessionRegistry,
    get_browser_session_registry,
)
from src.core.run_workspace import get_current_workspace
from src.tools.base_tool import BaseTool, ToolParams, ToolResult


BrowserAction = Literal[
    "start",
    "list",
    "navigate",
    "snapshot",
    "click",
    "type",
    "key",
    "storage",
    "sources",
    "network",
    "screenshot",
    "evaluate",
    "close",
]


class BrowserToolParams(ToolParams):
    action: BrowserAction = Field(
        description=(
            "Browser action: start, list, navigate, snapshot, click, type, key, "
            "storage, sources, network, screenshot, evaluate, or close."
        )
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Playwright CLI session name. Required for all actions except start "
            "and list. If omitted for start, Kerkov creates one."
        ),
    )
    url: Optional[str] = Field(
        default=None,
        description="URL for start/navigate, or source URL for sources fetch.",
    )
    headless: bool = Field(
        default=True,
        description="Launch browser headless for start action.",
    )
    viewport_width: int = Field(
        default=1280,
        description="Browser viewport width for start action.",
    )
    viewport_height: int = Field(
        default=720,
        description="Browser viewport height for start action.",
    )
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = Field(
        default="domcontentloaded",
        description=(
            "Accepted for compatibility. Playwright CLI controls navigation waits."
        ),
    )
    timeout: Optional[float] = Field(
        default=None,
        description="Subprocess timeout in seconds. Defaults to 30 seconds.",
    )
    selector: Optional[str] = Field(
        default=None,
        description="CSS selector for click/type/screenshot target.",
    )
    ref: Optional[str] = Field(
        default=None,
        description=(
            "Element ref returned by Playwright CLI snapshot, such as e6. "
            "The wrapper also accepts [ref=e6]."
        ),
    )
    text: Optional[str] = Field(
        default=None,
        description="Text to type/fill for type action.",
    )
    clear_first: bool = Field(
        default=True,
        description=(
            "Use Playwright CLI fill for type action. If false, click target and "
            "then type text into the focused element."
        ),
    )
    keys: Optional[str] = Field(
        default=None,
        description=(
            "Keys to press for key action, such as Enter, Tab, Escape, Control+A."
        ),
    )
    kind: Literal["all", "localStorage", "sessionStorage", "cookies"] = Field(
        default="all",
        description="Storage kind to inspect for storage action.",
    )
    include_values: bool = Field(
        default=True,
        description=(
            "Include storage/cookie values. Set false to list keys/metadata only."
        ),
    )
    limit: int = Field(
        default=50,
        description=(
            "Maximum source or network entries to return when the backend supports it."
        ),
    )
    full_page: bool = Field(
        default=False,
        description="Capture full page for screenshot action.",
    )
    path: Optional[str] = Field(
        default=None,
        description=(
            "Destination screenshot path. Defaults to <workspace>/browser/screenshots."
        ),
    )
    script: Optional[str] = Field(
        default=None,
        description="JavaScript expression/function to run for evaluate action.",
    )
    all_sessions: bool = Field(
        default=False,
        description="Close all Playwright CLI browser sessions for close action.",
    )


@dataclass
class PlaywrightCliResult:
    command: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    payload: Any

    @property
    def failed(self) -> bool:
        return self.returncode != 0 or (
            isinstance(self.payload, dict) and self.payload.get("isError") is True
        )

    @property
    def error_message(self) -> str:
        if isinstance(self.payload, dict) and self.payload.get("error"):
            return str(self.payload["error"])
        if self.stderr.strip():
            return self.stderr.strip()
        if self.stdout.strip():
            return self.stdout.strip()
        return f"Playwright CLI exited with code {self.returncode}"


class PlaywrightCliRunnerProtocol(Protocol):
    async def run(
        self, args: Sequence[str], cwd: Path, timeout_s: float
    ) -> PlaywrightCliResult:
        ...


class PlaywrightCliRunner:
    """Run Playwright CLI commands as short subprocesses."""

    def __init__(self, command_prefix: Optional[Sequence[str]] = None) -> None:
        self.command_prefix = list(command_prefix or _resolve_playwright_cli_command())

    async def run(
        self, args: Sequence[str], cwd: Path, timeout_s: float
    ) -> PlaywrightCliResult:
        cwd.mkdir(parents=True, exist_ok=True)
        command = [*self.command_prefix, *args, "--json"]

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return PlaywrightCliResult(
                command=command,
                cwd=cwd,
                returncode=127,
                stdout="",
                stderr=(
                    "Playwright CLI was not found. Install it with "
                    "`npm install -g @playwright/cli@latest`, set "
                    "KERKOV_PLAYWRIGHT_CLI, or install npm/npx for fallback."
                ),
                payload=None,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return PlaywrightCliResult(
                command=command,
                cwd=cwd,
                returncode=-1,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=(
                    stderr.decode("utf-8", errors="replace")
                    + f"\nTimed out after {timeout_s:.1f} seconds"
                ),
                payload=None,
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        return PlaywrightCliResult(
            command=command,
            cwd=cwd,
            returncode=process.returncode or 0,
            stdout=stdout_text,
            stderr=stderr_text,
            payload=_parse_json_or_text(stdout_text),
        )


class BrowserTool(BaseTool):
    """Control and inspect a browser through Playwright CLI."""

    DEFAULT_TIMEOUT_S = 30.0
    MAX_LIMIT = 200
    SOURCE_CONTENT_CHAR_LIMIT = 50_000

    def __init__(
        self,
        registry: Optional[BrowserSessionRegistry] = None,
        screenshot_dir: Optional[Path] = None,
        cli_work_dir: Optional[Path] = None,
        cli_runner: Optional[PlaywrightCliRunnerProtocol] = None,
    ) -> None:
        super().__init__(
            name="browser",
            description=(
                "Interact with a Playwright CLI browser session. Actions: start, "
                "list, navigate, snapshot, click, type, key, storage, sources, "
                "network, screenshot, evaluate, close. Use snapshot to get "
                "Playwright refs like e6 before click/type. Screenshots are "
                "constrained to the browser screenshot directory."
            ),
        )
        self.registry = registry or get_browser_session_registry()
        self.screenshot_dir = screenshot_dir or Path("data/browser/screenshots")
        self.cli_work_dir = cli_work_dir or Path("data/browser/playwright-cli")
        self.cli_runner = cli_runner or PlaywrightCliRunner()

    def get_params_class(self) -> Type[ToolParams]:
        return BrowserToolParams

    def validate_params(self, params: BrowserToolParams) -> Optional[str]:
        if params.timeout is not None and params.timeout <= 0:
            return "Timeout must be greater than 0 seconds"
        if params.viewport_width <= 0 or params.viewport_height <= 0:
            return "Viewport dimensions must be positive"
        if params.limit <= 0:
            return "Limit must be greater than 0"

        if params.action not in ("start", "list") and not params.session_id:
            if not (params.action == "close" and params.all_sessions):
                return "session_id is required for this browser action"

        if params.action in ("start", "navigate") and params.url:
            if not _is_browser_url(params.url):
                return "URL must start with http://, https://, file://, or about:"
        if params.action == "sources" and params.url:
            if not params.url.strip().lower().startswith(("http://", "https://")):
                return "Source URL must start with http:// or https://"

        if params.action == "navigate" and not params.url:
            return "url is required for navigate"
        if params.action in ("click", "type") and not (params.ref or params.selector):
            return "Provide either ref or selector"
        if params.action == "type" and params.text is None:
            return "text is required for type"
        if params.action == "key" and not params.keys:
            return "keys is required for key"
        if params.action == "evaluate" and not params.script:
            return "script is required for evaluate"
        return None

    async def run(self, params: BrowserToolParams) -> ToolResult:
        try:
            await self._cleanup_expired_sessions(params)

            if params.action == "start":
                return await self._handle_start(params)
            if params.action == "list":
                return await self._handle_list(params)
            if params.action == "close":
                return await self._handle_close(params)

            session = self._get_or_create_session(params.session_id or "")

            if params.action == "navigate":
                return await self._handle_navigate(session, params)
            if params.action == "snapshot":
                return await self._handle_snapshot(session, params)
            if params.action == "click":
                return await self._handle_click(session, params)
            if params.action == "type":
                return await self._handle_type(session, params)
            if params.action == "key":
                return await self._handle_key(session, params)
            if params.action == "storage":
                return await self._handle_storage(session, params)
            if params.action == "sources":
                return await self._handle_sources(session, params)
            if params.action == "network":
                return await self._handle_network(session, params)
            if params.action == "screenshot":
                return await self._handle_screenshot(session, params)
            if params.action == "evaluate":
                return await self._handle_evaluate(session, params)

            return ToolResult(
                success=False, output="", error=f"Unknown action: {params.action}"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Browser tool error: {e}",
            )

    async def _handle_start(self, params: BrowserToolParams) -> ToolResult:
        session_id = params.session_id or self.registry.create_session_id()
        session = BrowserSession(
            id=session_id,
            started_at=time.time(),
            headless=params.headless,
            work_dir=self._cli_work_root(),
            url=params.url or "",
        )

        args = [f"-s={session.id}", "open"]
        if params.url:
            args.append(params.url)
        if not params.headless:
            args.append("--headed")

        open_result = await self._run_cli(args, session, params)
        if open_result.failed:
            return _cli_error_result(open_result)

        payload = _payload_dict(open_result)
        session.pid = _extract_pid(payload)
        self.registry.add(session)

        resize_result = await self._run_session_cli(
            session,
            ["resize", str(params.viewport_width), str(params.viewport_height)],
            params,
        )
        if resize_result.failed:
            self.registry.remove(session.id)
            await self._run_cli([f"-s={session.id}", "close"], session, params)
            return _cli_error_result(resize_result)

        return _json_result(
            True,
            {
                "session_id": session.id,
                "headless": params.headless,
                "backend": "playwright-cli",
                "work_dir": str(session.work_dir),
                "open": _payload_or_text(open_result),
                "resize": _payload_or_text(resize_result),
            },
            metadata={"session_id": session.id, "backend": "playwright-cli"},
        )

    async def _handle_list(self, params: BrowserToolParams) -> ToolResult:
        result = await self._run_cli(["list"], self._default_cli_session(), params)
        if result.failed:
            return _cli_error_result(result)

        payload = _payload_dict(result)
        browsers = payload.get("browsers", []) if isinstance(payload, dict) else []
        sessions = [
            {
                "session_id": item.get("name"),
                "status": item.get("status"),
                "browser_type": item.get("browserType"),
                "headed": item.get("headed"),
                "persistent": item.get("persistent"),
                "attached": item.get("attached"),
                "compatible": item.get("compatible"),
                "version": item.get("version"),
            }
            for item in browsers
            if isinstance(item, dict)
        ]
        return _json_result(
            True,
            {
                "sessions": sessions,
                "count": len(sessions),
                "backend": "playwright-cli",
                "raw": _payload_or_text(result),
            },
        )

    async def _handle_close(self, params: BrowserToolParams) -> ToolResult:
        if params.all_sessions:
            result = await self._run_cli(
                ["close-all"], self._default_cli_session(), params
            )
            if result.failed:
                return _cli_error_result(result)
            closed = [session.id for session in self.registry.list()]
            for session_id in closed:
                self.registry.remove(session_id)
            return _json_result(
                True,
                {
                    "closed": closed,
                    "backend": "playwright-cli",
                    "cli": _payload_or_text(result),
                },
            )

        session = self._get_or_create_session(params.session_id or "")
        result = await self._run_session_cli(session, ["close"], params)
        if result.failed:
            return _cli_error_result(result)
        self.registry.remove(session.id)
        return _json_result(
            True,
            {
                "closed": [session.id],
                "backend": "playwright-cli",
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_navigate(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        result = await self._run_session_cli(
            session, ["goto", params.url or ""], params
        )
        if result.failed:
            return _cli_error_result(result)
        session.url = params.url or session.url
        self.registry.add(session)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "url": session.url,
                "backend": "playwright-cli",
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_snapshot(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        args = ["snapshot"]
        target = _optional_target_from_params(params)
        if target:
            args.append(target)
        result = await self._run_session_cli(session, args, params)
        if result.failed:
            return _cli_error_result(result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "backend": "playwright-cli",
                **_payload_dict(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_click(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        target = _target_from_params(params)
        result = await self._run_session_cli(session, ["click", target], params)
        if result.failed:
            return _cli_error_result(result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "clicked": target,
                "backend": "playwright-cli",
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_type(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        target = _target_from_params(params)
        if params.clear_first:
            result = await self._run_session_cli(
                session,
                ["fill", target, params.text or ""],
                params,
            )
            if result.failed:
                return _cli_error_result(result)
            return _json_result(
                True,
                {
                    "session_id": session.id,
                    "typed": target,
                    "mode": "fill",
                    "backend": "playwright-cli",
                    "cli": _payload_or_text(result),
                },
                metadata={"session_id": session.id},
            )

        click_result = await self._run_session_cli(session, ["click", target], params)
        if click_result.failed:
            return _cli_error_result(click_result)
        type_result = await self._run_session_cli(
            session,
            ["type", params.text or ""],
            params,
        )
        if type_result.failed:
            return _cli_error_result(type_result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "typed": target,
                "mode": "click_then_type",
                "backend": "playwright-cli",
                "click": _payload_or_text(click_result),
                "type": _payload_or_text(type_result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_key(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        result = await self._run_session_cli(
            session, ["press", params.keys or ""], params
        )
        if result.failed:
            return _cli_error_result(result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "pressed": params.keys,
                "backend": "playwright-cli",
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_storage(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        data: dict[str, Any] = {}

        if params.kind in ("all", "localStorage"):
            local_storage = await self._eval_json(
                session,
                (
                    "() => JSON.stringify("
                    "Object.fromEntries(Object.entries(localStorage)))"
                ),
                params,
            )
            if isinstance(local_storage, ToolResult):
                return local_storage
            data["localStorage"] = local_storage

        if params.kind in ("all", "sessionStorage"):
            session_storage = await self._eval_json(
                session,
                (
                    "() => JSON.stringify("
                    "Object.fromEntries(Object.entries(sessionStorage)))"
                ),
                params,
            )
            if isinstance(session_storage, ToolResult):
                return session_storage
            data["sessionStorage"] = session_storage

        if params.kind in ("all", "cookies"):
            cookies = await self._read_cookies(session, params)
            if isinstance(cookies, ToolResult):
                return cookies
            data["cookies"] = cookies

        if not params.include_values:
            data = _keys_only(data)

        return _json_result(
            True,
            {"session_id": session.id, "backend": "playwright-cli", **data},
            metadata={"session_id": session.id},
        )

    async def _handle_sources(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        if params.url:
            script = f"""
            () => fetch({json.dumps(params.url)}).then(async (response) => {{
              const text = await response.text();
              return JSON.stringify({{
                url: response.url,
                status: response.status,
                content_type: response.headers.get("content-type"),
                content: text.slice(0, {self.SOURCE_CONTENT_CHAR_LIMIT}),
                truncated: text.length > {self.SOURCE_CONTENT_CHAR_LIMIT},
                content_char_limit: {self.SOURCE_CONTENT_CHAR_LIMIT}
              }});
            }})
            """
            fetched = await self._eval_json(session, script, params)
            if isinstance(fetched, ToolResult):
                return fetched
            return _json_result(
                True,
                {"session_id": session.id, "backend": "playwright-cli", **fetched},
                metadata={"session_id": session.id},
            )

        source_data = await self._eval_json(
            session,
            """
            () => JSON.stringify({
              scripts: Array.from(document.scripts).map((s) => s.src).filter(Boolean),
              stylesheets: Array.from(
                document.querySelectorAll('link[rel~="stylesheet"]')
              )
                .map((l) => l.href).filter(Boolean)
            })
            """,
            params,
        )
        if isinstance(source_data, ToolResult):
            return source_data
        limit = min(params.limit, self.MAX_LIMIT)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "backend": "playwright-cli",
                "scripts": source_data.get("scripts", [])[:limit],
                "stylesheets": source_data.get("stylesheets", [])[:limit],
            },
            metadata={"session_id": session.id},
        )

    async def _handle_network(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        result = await self._run_session_cli(session, ["requests"], params)
        if result.failed:
            return _cli_error_result(result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "backend": "playwright-cli",
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_screenshot(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        path = self._resolve_screenshot_path(session, params)
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["screenshot"]
        target = _optional_target_from_params(params)
        if target:
            args.append(target)
        args.extend(["--filename", str(path)])
        if params.full_page:
            args.append("--full-page")

        result = await self._run_session_cli(session, args, params)
        if result.failed:
            return _cli_error_result(result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "backend": "playwright-cli",
                "path": str(path),
                "full_page": params.full_page,
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id},
        )

    async def _handle_evaluate(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> ToolResult:
        result = await self._run_session_cli(
            session, ["eval", params.script or ""], params
        )
        if result.failed:
            return _cli_error_result(result)
        return _json_result(
            True,
            {
                "session_id": session.id,
                "backend": "playwright-cli",
                "cli": _payload_or_text(result),
            },
            metadata={"session_id": session.id, "risk": "arbitrary_page_javascript"},
        )

    async def _eval_json(
        self,
        session: BrowserSession,
        script: str,
        params: BrowserToolParams,
    ) -> Any:
        result = await self._run_session_cli(session, ["eval", script], params)
        if result.failed:
            return _cli_error_result(result)
        payload = _payload_dict(result)
        raw_result = payload.get("result")
        if isinstance(raw_result, str):
            parsed = _parse_json_or_text(raw_result)
            return parsed
        return raw_result

    async def _read_cookies(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> Any:
        state_path = session.work_dir / ".playwright-cli" / f"{session.id}-state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        result = await self._run_session_cli(
            session,
            ["state-save", str(state_path)],
            params,
        )
        if result.failed:
            return _cli_error_result(result)
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except OSError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Could not read Playwright storage state: {e}",
            )
        return data.get("cookies", []) if isinstance(data, dict) else []

    def _get_or_create_session(self, session_id: str) -> BrowserSession:
        session = self.registry.get(session_id)
        if session is not None:
            return session
        return BrowserSession(
            id=session_id,
            started_at=time.time(),
            headless=True,
            work_dir=self._cli_work_root(),
        )

    async def _cleanup_expired_sessions(self, params: BrowserToolParams) -> None:
        expired_sessions = self.registry.prune_expired()
        for session in expired_sessions:
            self.logger.info(f"Pruned expired browser session: id={session.id}")
            await self._run_cli([f"-s={session.id}", "close"], session, params)

    async def _run_session_cli(
        self,
        session: BrowserSession,
        args: Sequence[str],
        params: BrowserToolParams,
    ) -> PlaywrightCliResult:
        return await self._run_cli([f"-s={session.id}", *args], session, params)

    async def _run_cli(
        self,
        args: Sequence[str],
        session: BrowserSession,
        params: BrowserToolParams,
    ) -> PlaywrightCliResult:
        return await self.cli_runner.run(args, session.work_dir, _timeout_s(params))

    def _default_cli_session(self) -> BrowserSession:
        return BrowserSession(
            id="default",
            started_at=time.time(),
            headless=True,
            work_dir=self._cli_work_root(),
        )

    def _resolve_screenshot_path(
        self, session: BrowserSession, params: BrowserToolParams
    ) -> Path:
        screenshot_root = self._screenshot_root()
        if params.path:
            requested = Path(params.path).expanduser()
            path = (
                requested.resolve()
                if requested.is_absolute()
                else (screenshot_root / requested).resolve()
            )
            try:
                path.relative_to(screenshot_root)
            except ValueError:
                raise ValueError(
                    f"Screenshot path must be under {screenshot_root}"
                )
            return path
        return (
            screenshot_root / f"browser-{session.id}-{int(time.time())}.png"
        ).resolve()

    def _screenshot_root(self) -> Path:
        workspace = get_current_workspace()
        if workspace is not None:
            return (workspace / "browser" / "screenshots").expanduser().resolve()
        return self.screenshot_dir.expanduser().resolve()

    def _cli_work_root(self) -> Path:
        workspace = get_current_workspace()
        if workspace is not None:
            return (workspace / "browser" / "playwright-cli").expanduser().resolve()
        return self.cli_work_dir.expanduser().resolve()


def _timeout_s(params: BrowserToolParams) -> float:
    if params.timeout is not None:
        return params.timeout
    return BrowserTool.DEFAULT_TIMEOUT_S


def _is_browser_url(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith(("http://", "https://", "file://", "about:"))


def _target_from_params(params: BrowserToolParams) -> str:
    target = params.ref or params.selector
    assert target is not None
    return _normalize_target(target)


def _optional_target_from_params(params: BrowserToolParams) -> Optional[str]:
    target = params.ref or params.selector
    if target is None:
        return None
    return _normalize_target(target)


def _normalize_target(target: str) -> str:
    target = target.strip()
    if target.startswith("[ref=") and target.endswith("]"):
        return target[len("[ref=") : -1]
    return target


def _keys_only(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in data.items():
        if isinstance(value, dict):
            result[name] = sorted(value.keys())
        elif isinstance(value, list):
            result[name] = [_safe_cookie_metadata(item) for item in value]
        else:
            result[name] = value
    return result


def _safe_cookie_metadata(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {
        key: item[key]
        for key in (
            "name",
            "domain",
            "path",
            "expires",
            "httpOnly",
            "secure",
            "sameSite",
        )
        if key in item
    }


def _resolve_playwright_cli_command() -> list[str]:
    configured = os.environ.get("KERKOV_PLAYWRIGHT_CLI")
    if configured:
        return shlex.split(configured)

    local_binary = Path("node_modules") / ".bin" / "playwright-cli"
    if local_binary.exists():
        return [str(local_binary.resolve())]

    global_binary = shutil.which("playwright-cli")
    if global_binary:
        return [global_binary]

    if shutil.which("npx"):
        return ["npx", "-y", "@playwright/cli@latest"]

    return ["playwright-cli"]


def _extract_pid(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        pid = payload.get("pid")
        return pid if isinstance(pid, int) else None
    return None


def _parse_json_or_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _payload_dict(result: PlaywrightCliResult) -> dict[str, Any]:
    if isinstance(result.payload, dict):
        return result.payload
    return {"result": result.payload}


def _payload_or_text(result: PlaywrightCliResult) -> Any:
    if result.payload is not None:
        return result.payload
    return result.stdout.strip()


def _cli_error_result(result: PlaywrightCliResult) -> ToolResult:
    return ToolResult(
        success=False,
        output="",
        error=result.error_message,
        metadata={
            "backend": "playwright-cli",
            "command": result.command,
            "cwd": str(result.cwd),
            "exit_code": result.returncode,
        },
    )


def _json_result(
    success: bool,
    payload: Any,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> ToolResult:
    return ToolResult(
        success=success,
        output=json.dumps(payload, indent=2, sort_keys=True, default=str),
        metadata=metadata,
    )
