"""OWASP ZAP crawling, active scanning, and alert retrieval tool."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Literal, Optional, Type

from pydantic import Field

from kerkov.core.config_manager import get_config
from kerkov.core.credentials import (
    ZAP_API_KEY,
    CredentialManager,
    get_credential_manager,
)
from kerkov.core.proxy import (
    InterceptingProxyAdapter,
    ProxyAdapterError,
    ProxyIssue,
    ProxyScanLaunch,
    ProxyScanProgress,
    ScanKind,
    ZapAdapter,
    normalize_scan_target,
)
from kerkov.tools.base_tool import BaseTool, ToolParams, ToolResult

ZapAction = Literal[
    "status",
    "spider_start",
    "spider_status",
    "active_scan_start",
    "active_scan_status",
    "alerts",
    "scan_stop",
]
ZapScanKind = Literal["spider", "active"]
MAX_ISSUE_TEXT_CHARS = 2000


class ZapToolParams(ToolParams):
    """Parameters for one ZAP API operation."""

    action: ZapAction = Field(
        description=(
            "Operation: status, spider_start, spider_status, active_scan_start, "
            "active_scan_status, alerts, or scan_stop. Spider a target before "
            "starting an active scan so ZAP knows its reachable resources."
        )
    )
    target: Optional[str] = Field(
        default=None,
        description=(
            "Absolute http(s) target URL. Required for spider_start and "
            "active_scan_start; optional base URL filter for alerts."
        ),
        max_length=4096,
    )
    scan_id: Optional[str] = Field(
        default=None,
        description="Numeric scan identifier returned by a start action.",
        max_length=64,
    )
    scan_kind: Optional[ZapScanKind] = Field(
        default=None,
        description="For scan_stop, whether the id belongs to spider or active.",
    )
    recurse: bool = Field(
        default=True,
        description="Recursively crawl or actively scan child resources.",
    )
    subtree_only: bool = Field(
        default=True,
        description="For spider_start, constrain crawling below the target path.",
    )
    max_children: Optional[int] = Field(
        default=None,
        description="Optional maximum spider children per node.",
        ge=0,
        le=10000,
    )
    in_scope_only: bool = Field(
        default=False,
        description=(
            "For active_scan_start, restrict scanning to URLs already in ZAP's "
            "configured scope. Leave false unless a ZAP context is configured."
        ),
    )
    scan_policy: Optional[str] = Field(
        default=None,
        description="Optional named ZAP active scan policy.",
        max_length=256,
    )
    start: int = Field(
        default=0,
        description="For alerts, zero-based result offset.",
        ge=0,
    )
    count: int = Field(
        default=25,
        description="For alerts, maximum findings to return.",
        ge=1,
        le=100,
    )


class ZapTool(BaseTool):
    """Expose a configured OWASP ZAP daemon to Kerkov."""

    def __init__(
        self,
        *,
        credentials: Optional[CredentialManager] = None,
        adapter: Optional[InterceptingProxyAdapter] = None,
    ) -> None:
        super().__init__(
            name="zap",
            description=(
                "Control Kerkov's OWASP ZAP scanner. Use status to verify the "
                "daemon, spider_start then spider_status to discover and passively "
                "scan a target, active_scan_start then active_scan_status for "
                "intrusive testing, alerts to retrieve findings, and scan_stop to "
                "stop a running scan. Active scanning can modify target state and "
                "must only be used on explicitly authorized targets. Run /zap setup "
                "once if the daemon or API key is not configured."
            ),
        )
        self._credentials = credentials or get_credential_manager()
        self._injected_adapter = adapter

    def get_params_class(self) -> Type[ToolParams]:
        return ZapToolParams

    def validate_params(self, toolParams: ToolParams) -> Optional[str]:
        if not isinstance(toolParams, ZapToolParams):
            return "invalid ZAP parameters"
        if toolParams.action in {"spider_start", "active_scan_start"}:
            if not toolParams.target:
                return f"target is required for {toolParams.action}"
        if toolParams.action in {
            "spider_status",
            "active_scan_status",
            "scan_stop",
        }:
            if not toolParams.scan_id:
                return f"scan_id is required for {toolParams.action}"
            if not toolParams.scan_id.strip().isdigit():
                return "scan_id must contain only digits"
        if toolParams.action == "scan_stop" and toolParams.scan_kind is None:
            return "scan_kind is required for scan_stop"
        if toolParams.target:
            try:
                normalize_scan_target(toolParams.target)
            except ValueError as exc:
                return str(exc)
        return None

    async def run(self, toolParams: ToolParams) -> ToolResult:
        if not isinstance(toolParams, ZapToolParams):
            raise TypeError("expected ZapToolParams")
        try:
            adapter = self._adapter()
            if toolParams.action == "status":
                info = await adapter.probe()
                return self._success(toolParams.action, asdict(info))
            if toolParams.action == "spider_start":
                launch = await adapter.start_spider(
                    toolParams.target or "",
                    recurse=toolParams.recurse,
                    subtree_only=toolParams.subtree_only,
                    max_children=toolParams.max_children,
                )
                return self._success(toolParams.action, _serialize(launch))
            if toolParams.action == "active_scan_start":
                launch = await adapter.start_active_scan(
                    toolParams.target or "",
                    recurse=toolParams.recurse,
                    in_scope_only=toolParams.in_scope_only,
                    scan_policy=toolParams.scan_policy,
                )
                return self._success(toolParams.action, _serialize(launch))
            if toolParams.action in {"spider_status", "active_scan_status"}:
                kind = (
                    ScanKind.SPIDER
                    if toolParams.action == "spider_status"
                    else ScanKind.ACTIVE
                )
                progress = await adapter.get_scan(toolParams.scan_id or "", kind)
                return self._success(toolParams.action, _serialize(progress))
            if toolParams.action == "alerts":
                issues = await adapter.get_issues(
                    target=toolParams.target,
                    start=toolParams.start,
                    count=toolParams.count,
                )
                payload = {
                    "count": len(issues),
                    "start": toolParams.start,
                    "issues": [_bounded_issue(issue) for issue in issues],
                }
                return self._success(toolParams.action, payload)

            kind = ScanKind(toolParams.scan_kind or "")
            await adapter.stop_scan(toolParams.scan_id or "", kind)
            return self._success(
                toolParams.action,
                {
                    "scan_id": toolParams.scan_id,
                    "kind": kind.value,
                    "state": "stopped",
                },
            )
        except (ProxyAdapterError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata={"provider": "zap", "action": toolParams.action},
            )

    def _adapter(self) -> InterceptingProxyAdapter:
        if self._injected_adapter is not None:
            return self._injected_adapter
        api_key = self._credentials.get(ZAP_API_KEY)
        if not api_key:
            raise ProxyAdapterError(
                "OWASP ZAP is not configured. Run /zap setup to start it securely."
            )
        config = get_config(reload=True)
        return ZapAdapter(
            service_url=config.zap_api_url,
            api_key=api_key,
            timeout_seconds=float(config.zap_api_timeout_seconds),
            verify_tls=config.zap_api_verify_tls,
            allow_remote=config.zap_api_allow_remote,
        )

    @staticmethod
    def _success(action: str, payload: object) -> ToolResult:
        output = json.dumps(payload, indent=2, sort_keys=True)
        return ToolResult(
            success=True,
            output=output,
            metadata={"provider": "zap", "action": action},
        )


def _serialize(
    value: ProxyScanLaunch | ProxyScanProgress,
) -> dict[str, object]:
    payload: dict[str, object] = dict(asdict(value))
    kind = payload.get("kind")
    if isinstance(kind, ScanKind):
        payload["kind"] = kind.value
    return payload


def _bounded_issue(issue: ProxyIssue) -> dict[str, object]:
    payload: dict[str, object] = dict(asdict(issue))
    for key, value in list(payload.items()):
        if isinstance(value, str) and len(value) > MAX_ISSUE_TEXT_CHARS:
            payload[key] = value[:MAX_ISSUE_TEXT_CHARS] + "…"
    return payload
