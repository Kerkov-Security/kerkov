"""Async adapter for OWASP ZAP's supported HTTP API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from kerkov.core.proxy.adapter import (
    InterceptingProxyAdapter,
    ProxyAdapterError,
    ProxyConnectionInfo,
    ProxyIssue,
    ProxyScanLaunch,
    ProxyScanProgress,
    ScanKind,
)

MAX_ERROR_BODY_CHARS = 800


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.strip("[]").lower()
    return normalized in {"localhost", "127.0.0.1", "::1"}


def normalize_zap_service_url(service_url: str, *, allow_remote: bool) -> str:
    """Validate a ZAP service origin and return its canonical form."""

    parsed = urlsplit(service_url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("ZAP_API_URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("ZAP_API_URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ZAP_API_URL must not include credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ZAP_API_URL must include a valid port") from exc
    if port is None:
        raise ValueError("ZAP_API_URL must include a port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            "ZAP_API_URL must be an origin without a path, query, or fragment"
        )
    if not allow_remote and not _is_loopback_host(parsed.hostname):
        raise ValueError(
            "ZAP_API_URL must use a loopback host unless ZAP_API_ALLOW_REMOTE=true"
        )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", "")).rstrip("/")


def normalize_scan_target(target: str) -> str:
    """Validate a web target without changing its meaningful path or query."""

    entered = target.strip()
    parsed = urlsplit(entered)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Scan target must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Scan target must not contain embedded credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Scan target contains an invalid port") from exc
    if parsed.fragment:
        raise ValueError("Scan target must not contain a URL fragment")
    return entered


class ZapAdapter(InterceptingProxyAdapter):
    """Thin adapter over ZAP's JSON API."""

    def __init__(
        self,
        *,
        service_url: str,
        api_key: str,
        timeout_seconds: float,
        verify_tls: bool = True,
        allow_remote: bool = False,
        loopback_target_host: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("ZAP API key must not be empty")
        self.service_url = normalize_zap_service_url(
            service_url,
            allow_remote=allow_remote,
        )
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._verify_tls = verify_tls
        self._loopback_target_host = (
            _normalize_target_host_alias(loopback_target_host)
            if loopback_target_host is not None
            else None
        )
        self._transport = transport

    async def probe(self) -> ProxyConnectionInfo:
        payload = await self._request("core", "view", "version")
        version = self._required_string(payload, "version")
        return ProxyConnectionInfo(
            provider="zap",
            service_url=self.service_url,
            version=version,
        )

    async def start_spider(
        self,
        target: str,
        *,
        recurse: bool = True,
        subtree_only: bool = True,
        max_children: Optional[int] = None,
    ) -> ProxyScanLaunch:
        normalized_target = normalize_scan_target(target)
        params: Dict[str, Any] = {
            "url": self._target_for_zap(normalized_target),
            "recurse": _zap_bool(recurse),
            "subtreeOnly": _zap_bool(subtree_only),
        }
        if max_children is not None:
            params["maxChildren"] = max_children
        payload = await self._request("spider", "action", "scan", params=params)
        scan_id = self._required_string(payload, "scan")
        return ProxyScanLaunch(
            scan_id=scan_id,
            kind=ScanKind.SPIDER,
            target=normalized_target,
        )

    async def start_active_scan(
        self,
        target: str,
        *,
        recurse: bool = True,
        in_scope_only: bool = False,
        scan_policy: Optional[str] = None,
    ) -> ProxyScanLaunch:
        normalized_target = normalize_scan_target(target)
        params: Dict[str, Any] = {
            "url": self._target_for_zap(normalized_target),
            "recurse": _zap_bool(recurse),
            "inScopeOnly": _zap_bool(in_scope_only),
        }
        if scan_policy:
            params["scanPolicyName"] = scan_policy
        payload = await self._request("ascan", "action", "scan", params=params)
        scan_id = self._required_string(payload, "scan")
        return ProxyScanLaunch(
            scan_id=scan_id,
            kind=ScanKind.ACTIVE,
            target=normalized_target,
        )

    async def get_scan(
        self,
        scan_id: str,
        kind: ScanKind,
    ) -> ProxyScanProgress:
        normalized_id = _normalize_scan_id(scan_id)
        component = "spider" if kind == ScanKind.SPIDER else "ascan"
        payload = await self._request(
            component,
            "view",
            "status",
            params={"scanId": normalized_id},
        )
        raw_status = self._required_string(payload, "status")
        try:
            progress = int(raw_status)
        except ValueError as exc:
            raise ProxyAdapterError(
                f"ZAP returned an invalid {kind.value} progress value"
            ) from exc
        if progress < 0 or progress > 100:
            raise ProxyAdapterError(
                f"ZAP returned an out-of-range {kind.value} progress value"
            )
        state = "completed" if progress == 100 else "running"
        if kind == ScanKind.SPIDER and progress == 100:
            passive_payload = await self._request(
                "pscan",
                "view",
                "recordsToScan",
            )
            raw_pending = self._required_string(
                passive_payload,
                "recordsToScan",
            )
            try:
                pending = int(raw_pending)
            except ValueError as exc:
                raise ProxyAdapterError(
                    "ZAP returned an invalid passive scan queue value"
                ) from exc
            if pending < 0:
                raise ProxyAdapterError(
                    "ZAP returned an out-of-range passive scan queue value"
                )
            if pending > 0:
                progress = 99
                state = "passive_scanning"
        return ProxyScanProgress(
            scan_id=normalized_id,
            kind=kind,
            progress=progress,
            state=state,
        )

    async def stop_scan(self, scan_id: str, kind: ScanKind) -> None:
        normalized_id = _normalize_scan_id(scan_id)
        component = "spider" if kind == ScanKind.SPIDER else "ascan"
        await self._request(
            component,
            "action",
            "stop",
            params={"scanId": normalized_id},
        )

    async def get_issues(
        self,
        *,
        target: Optional[str] = None,
        start: int = 0,
        count: int = 25,
    ) -> List[ProxyIssue]:
        params: Dict[str, Any] = {"start": start, "count": count}
        if target:
            params["baseurl"] = self._target_for_zap(normalize_scan_target(target))
        payload = await self._request("alert", "view", "alerts", params=params)
        raw_alerts = payload.get("alerts")
        if not isinstance(raw_alerts, list):
            raise ProxyAdapterError("ZAP returned an invalid alerts response")
        return [
            self._parse_issue(item) for item in raw_alerts if isinstance(item, dict)
        ]

    def _target_for_zap(self, normalized_target: str) -> str:
        if self._loopback_target_host is None:
            return normalized_target
        parsed = urlsplit(normalized_target)
        if not parsed.hostname or not _is_loopback_host(parsed.hostname):
            return normalized_target
        port_suffix = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(
            (
                parsed.scheme,
                f"{self._loopback_target_host}{port_suffix}",
                parsed.path,
                parsed.query,
                "",
            )
        )

    async def _request(
        self,
        component: str,
        operation_type: str,
        operation: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        path = f"/JSON/{component}/{operation_type}/{operation}/"
        headers = {
            "Accept": "application/json",
            "X-ZAP-API-Key": self._api_key,
        }
        if self._loopback_target_host is not None:
            # A managed container can publish its fixed internal API port on a
            # different host port. ZAP recognizes its reserved API hostname
            # independently of that Docker port translation.
            headers["Host"] = "zap"
        try:
            async with httpx.AsyncClient(
                base_url=self.service_url,
                headers=headers,
                timeout=self._timeout_seconds,
                verify=self._verify_tls,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ProxyAdapterError("Timed out connecting to the ZAP API") from exc
        except httpx.RequestError as exc:
            raise ProxyAdapterError("Could not connect to the ZAP API") from exc

        if not response.is_success:
            error_code = _safe_error_code(response)
            detail = _safe_error_detail(response).replace(
                self._api_key,
                "[REDACTED]",
            )
            suffix = f": {detail}" if detail else ""
            error = ProxyAdapterError(
                f"ZAP API returned HTTP {response.status_code}{suffix}",
                status_code=response.status_code,
                is_auth_failure=error_code == "bad_api_key",
            )
            raise error
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProxyAdapterError("ZAP API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProxyAdapterError("ZAP API returned an unexpected response")
        if "code" in payload and "message" in payload:
            error_code = str(payload.get("code") or "").strip().lower()
            message = str(payload.get("message") or "API request failed").replace(
                self._api_key,
                "[REDACTED]",
            )
            raise ProxyAdapterError(
                f"ZAP API error: {message}",
                is_auth_failure=error_code == "bad_api_key",
            )
        return payload

    @staticmethod
    def _required_string(payload: Dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, (str, int)) or not str(value).strip():
            raise ProxyAdapterError(f"ZAP response did not include {key!r}")
        return str(value).strip()

    @staticmethod
    def _parse_issue(item: Dict[str, Any]) -> ProxyIssue:
        return ProxyIssue(
            title=str(item.get("alert") or item.get("name") or "ZAP alert"),
            risk=str(item.get("risk") or "Unknown"),
            confidence=str(item.get("confidence") or "Unknown"),
            url=str(item.get("url") or ""),
            method=_optional_string(item.get("method")),
            parameter=_optional_string(item.get("param")),
            description=_optional_string(item.get("description")),
            solution=_optional_string(item.get("solution")),
            evidence=_optional_string(item.get("evidence")),
            cwe_id=_optional_string(item.get("cweid")),
            wasc_id=_optional_string(item.get("wascid")),
            reference=_optional_string(item.get("reference")),
        )


def _zap_bool(value: bool) -> str:
    return "true" if value else "false"


def _normalize_target_host_alias(hostname: str) -> str:
    normalized = hostname.strip().lower()
    parsed = urlsplit(f"http://{normalized}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "ZAP target host alias must be a hostname without a port"
        ) from exc
    if (
        not normalized
        or parsed.hostname != normalized
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ZAP target host alias must be a hostname without a port")
    return normalized


def _normalize_scan_id(scan_id: str) -> str:
    normalized = scan_id.strip()
    if not normalized or not normalized.isdigit():
        raise ValueError("scan_id must contain only digits")
    return normalized


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:MAX_ERROR_BODY_CHARS]
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("detail")
        if detail:
            return str(detail).strip()[:MAX_ERROR_BODY_CHARS]
    return ""


def _safe_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("code") or "").strip().lower()
