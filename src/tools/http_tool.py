"""
HTTP request tool with optional credential profiles.

Replaces FetchTool. Supports the full GET/POST/PUT/PATCH/DELETE method set,
JSON body and JSON response parsing, and named credential profiles loaded
from ``config/http_profiles.yaml``. Profile callers reference profiles by
name; the resolved Authorization/api-key headers never appear in tool
parameter logs.
"""

import json as json_lib
import re
import warnings
from typing import Any, Dict, Literal, Optional

import httpx
from pydantic import Field

from src.core.config_manager import get_config
from src.core.logging.logger_factory import get_logger
from src.tools.base_tool import BaseTool, ToolApprovalLevel, ToolParams, ToolResult
from src.tools.http_profile_loader import ResolvedProfile

logger = get_logger("HTTPTool")

_SECRET_HEADER_RE = re.compile(r"(?i)authorization|api[-_]?key|x-.*-token|cookie")


# `json` is part of the LLM-facing tool schema; it shadows BaseModel.json (deprecated in Pydantic v2).
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "json" .*shadows an attribute',
        category=UserWarning,
    )

    class HTTPToolParams(ToolParams):
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(
            description="HTTP method: GET, POST, PUT, PATCH, or DELETE"
        )
        profile: Optional[str] = Field(
            default=None,
            description=(
                "Named credential profile (e.g. 'htb'). Pair with `path`. The "
                "profile supplies base URL, default headers, and timeout."
            ),
        )
        url: Optional[str] = Field(
            default=None,
            description=(
                "Full URL for ad-hoc requests. Mutually exclusive with `profile`."
            ),
        )
        path: Optional[str] = Field(
            default=None,
            description="Path to join with the profile's base_url. Required with `profile`.",
        )
        headers: Optional[Dict[str, str]] = Field(
            default=None,
            description="Additional headers (merged on top of profile headers).",
        )
        params: Optional[Dict[str, Any]] = Field(
            default=None,
            description="Query string parameters as key/value pairs.",
        )
        json: Optional[Any] = Field(
            default=None,
            description=(
                "JSON body. Sets Content-Type: application/json automatically. "
                "Mutually exclusive with `data`."
            ),
        )
        data: Optional[str] = Field(
            default=None,
            description="Raw text body. Mutually exclusive with `json`.",
        )
        timeout: Optional[float] = Field(
            default=None,
            description="Per-request timeout in seconds. Falls back to profile or config default.",
        )


class HTTPTool(BaseTool):
    """Send HTTP requests, optionally via a named credential profile."""

    MAX_RESPONSE_SIZE = 10 * 1024 * 1024

    def __init__(self, profiles: Optional[Dict[str, ResolvedProfile]] = None) -> None:
        super().__init__(
            name="http",
            description=(
                "Send HTTP/HTTPS requests. Supports GET, POST, PUT, PATCH, DELETE. "
                "Use `profile` + `path` for authenticated APIs (the profile supplies "
                "base URL, headers, and timeout); use `url` for ad-hoc requests. "
                "Set `json` for a JSON body (Content-Type set automatically) or `data` "
                "for a raw text body. Maximum response size is 10MB."
            ),
        )
        self.profiles: Dict[str, ResolvedProfile] = profiles or {}
        self.config = get_config()

    def get_approval_level(self) -> ToolApprovalLevel:
        return ToolApprovalLevel.OPTIONAL

    def get_params_class(self) -> type:
        return HTTPToolParams

    def validate_params(self, toolParams: HTTPToolParams) -> Optional[str]:
        has_profile = toolParams.profile is not None
        has_url = toolParams.url is not None
        has_path = toolParams.path is not None

        if has_profile == has_url:
            return "Specify exactly one of `profile` (with `path`) or `url`"
        if has_profile and not has_path:
            return "When using `profile`, also provide `path`"
        if has_url and has_path:
            return "Do not provide `path` when using a raw `url`"
        if has_profile and toolParams.profile not in self.profiles:
            known = ", ".join(sorted(self.profiles)) or "<none>"
            return (
                f"Unknown profile '{toolParams.profile}'. Known profiles: {known}"
            )

        if toolParams.json is not None and toolParams.data is not None:
            return "Specify either `json` or `data`, not both"

        if toolParams.timeout is not None and toolParams.timeout <= 0:
            return "Timeout must be greater than 0 seconds"

        if has_url:
            url = (toolParams.url or "").strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                return "URL must start with http:// or https://"

        if has_path:
            path = (toolParams.path or "").strip()
            if path.startswith("http://") or path.startswith("https://"):
                return (
                    "`path` must be relative to the profile's base_url. "
                    "Use `url` for absolute URLs."
                )

        return None

    async def run(self, toolParams: HTTPToolParams) -> ToolResult:
        method = toolParams.method

        if toolParams.profile is not None:
            profile = self.profiles[toolParams.profile]
            url = self._join_url(profile.base_url, toolParams.path or "")
            merged_headers: Dict[str, str] = dict(profile.headers)
            request_timeout: float = toolParams.timeout or profile.timeout_seconds
        else:
            url = (toolParams.url or "").strip()
            merged_headers = {}
            request_timeout = toolParams.timeout or float(self.config.max_timeout_seconds)

        if toolParams.headers:
            merged_headers.update(toolParams.headers)

        body_kwargs: Dict[str, Any] = {}
        if toolParams.json is not None:
            body_kwargs["content"] = json_lib.dumps(
                toolParams.json, ensure_ascii=False
            ).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/json")
        elif toolParams.data is not None:
            body_kwargs["content"] = toolParams.data

        self._log_request(method, url, merged_headers, toolParams.params)

        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers=merged_headers,
                    params=toolParams.params,
                    **body_kwargs,
                )
                return self._process_response(response, url)
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=f"Request timed out after {request_timeout} seconds",
                metadata={"url": url, "timeout": request_timeout},
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"HTTP error: {e}",
                metadata={"url": url},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"HTTP request error: {e}",
                metadata={"url": url},
            )

    @staticmethod
    def _join_url(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        if not path:
            return base
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def _log_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        query: Optional[Dict[str, Any]],
    ) -> None:
        redacted = {k: self._redact_header(k, v) for k, v in headers.items()}
        logger.info(f"{method} {url} headers={redacted} params={query}")

    @staticmethod
    def _redact_header(key: str, value: str) -> str:
        if _SECRET_HEADER_RE.search(key):
            if len(value) <= 4:
                return "****"
            return f"****{value[-4:]}"
        return value

    def _process_response(self, response: httpx.Response, url: str) -> ToolResult:
        try:
            content = response.text

            if len(content) > self.MAX_RESPONSE_SIZE:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Response too large: {len(content)} bytes "
                        f"(max: {self.MAX_RESPONSE_SIZE} bytes)"
                    ),
                    metadata={
                        "url": url,
                        "status": response.status_code,
                        "content_length": len(content),
                    },
                )

            success = 200 <= response.status_code < 300
            metadata: Dict[str, Any] = {
                "url": url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "content_length": len(content),
                "headers": dict(response.headers),
            }

            content_type = (response.headers.get("content-type") or "").lower()
            if "application/json" in content_type and content:
                try:
                    metadata["parsed"] = json_lib.loads(content)
                except json_lib.JSONDecodeError:
                    pass

            return ToolResult(
                success=success,
                output=content,
                error=(
                    None
                    if success
                    else f"HTTP {response.status_code}: {response.reason_phrase}"
                ),
                metadata=metadata,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error processing response: {e}",
                metadata={"url": url, "status": response.status_code},
            )
