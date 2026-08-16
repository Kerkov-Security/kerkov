"""Intercepting proxy integrations."""

from kerkov.core.proxy.adapter import (
    InterceptingProxyAdapter,
    ProxyAdapterError,
    ProxyConnectionInfo,
    ProxyIssue,
    ProxyScanLaunch,
    ProxyScanProgress,
    ScanKind,
)
from kerkov.core.proxy.zap import (
    ZapAdapter,
    normalize_scan_target,
    normalize_zap_service_url,
)
from kerkov.core.proxy.zap_runtime import (
    MANAGED_HOST_ALIAS,
    DockerZapRuntime,
    ZapRuntimeError,
    ZapRuntimeInfo,
    choose_loopback_service_url,
)

__all__ = [
    "DockerZapRuntime",
    "InterceptingProxyAdapter",
    "MANAGED_HOST_ALIAS",
    "ProxyAdapterError",
    "ProxyConnectionInfo",
    "ProxyIssue",
    "ProxyScanLaunch",
    "ProxyScanProgress",
    "ScanKind",
    "ZapAdapter",
    "ZapRuntimeError",
    "ZapRuntimeInfo",
    "choose_loopback_service_url",
    "normalize_scan_target",
    "normalize_zap_service_url",
]
