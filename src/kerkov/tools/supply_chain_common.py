"""Shared source, artifact, and result helpers for Syft and Grype tools."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Optional, Tuple

from kerkov.core.anchore_runtime import AnchoreRuntimeInfo

MAX_FAILURE_TAIL_CHARS = 4000
MAX_ERROR_CHARS = 1200
MAX_SUMMARY_REPORT_BYTES = 16 * 1024 * 1024
MAX_TEXT_CHARS = 500

_AUTO_IMAGE_SOURCES = ("docker", "podman", "containerd", "registry")
_NON_IMAGE_SOURCE_SCHEMES = (
    "cpe",
    "dir",
    "docker-archive",
    "file",
    "oci-archive",
    "oci-dir",
    "purl",
    "sbom",
    "singularity",
)


class SupplyChainSourceType(str, Enum):
    """Explicit source kinds accepted by the supply-chain scanners."""

    DIRECTORY = "directory"
    FILE = "file"
    IMAGE = "image"
    SBOM = "sbom"


class ImageSource(str, Enum):
    """How an image should be loaded."""

    AUTO = "auto"
    DOCKER = "docker"
    REGISTRY = "registry"


class ImageScope(str, Enum):
    """Which container layers should be cataloged."""

    SQUASHED = "squashed"
    ALL_LAYERS = "all-layers"


@dataclass(frozen=True)
class ResolvedSupplyChainSource:
    """Validated CLI input and a safe display name for a scan source."""

    target: str
    from_values: Tuple[str, ...]
    display: str


def resolve_source(
    raw_target: str,
    source_type: SupplyChainSourceType,
    workspace: Path,
    image_source: ImageSource,
) -> ResolvedSupplyChainSource:
    """Resolve local paths inside the workspace or validate an image reference."""

    target = raw_target.strip()
    if source_type == SupplyChainSourceType.IMAGE:
        image_error = validate_image_reference(target)
        if image_error:
            raise ValueError(image_error)
        from_values = (
            _AUTO_IMAGE_SOURCES
            if image_source == ImageSource.AUTO
            else (image_source.value,)
        )
        return ResolvedSupplyChainSource(
            target=target,
            from_values=from_values,
            display=target,
        )
    if image_source != ImageSource.AUTO:
        raise ValueError("image_source can only be set when source_type=image")

    raw_path = Path(target).expanduser()
    candidate = raw_path if raw_path.is_absolute() else workspace / raw_path
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            f"{source_type.value} target resolves outside the active workspace"
        ) from exc
    if not resolved.exists():
        raise ValueError(f"{source_type.value} target does not exist: {resolved}")
    if source_type == SupplyChainSourceType.DIRECTORY and not resolved.is_dir():
        raise ValueError(f"directory target is not a directory: {resolved}")
    if source_type in {SupplyChainSourceType.FILE, SupplyChainSourceType.SBOM}:
        if not resolved.is_file():
            raise ValueError(f"{source_type.value} target is not a file: {resolved}")
    return ResolvedSupplyChainSource(
        target=str(resolved),
        from_values=(
            (
                "dir"
                if source_type == SupplyChainSourceType.DIRECTORY
                else source_type.value
            ),
        ),
        display=relative.as_posix() or ".",
    )


def validate_image_reference(value: str) -> Optional[str]:
    if not value:
        return "image target must not be empty"
    if len(value) > 1024:
        return "image target must be at most 1024 characters"
    if value.startswith("-"):
        return "image target must not begin with '-'"
    lowered = value.casefold()
    if any(lowered.startswith(f"{scheme}:") for scheme in _NON_IMAGE_SOURCE_SCHEMES):
        return "image target must not use a non-image source scheme"
    if (
        Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.startswith(("/", "\\", "./", "../", ".\\", "..\\", "~/", "~\\"))
    ):
        return "image target must not be a local filesystem path"
    if "://" in value or any(character.isspace() for character in value):
        return "image target must be an image reference, not a URL or whitespace"
    if "?" in value or "#" in value:
        return "image target must not contain URL query or fragment syntax"
    if "@" in value:
        digest = value.rsplit("@", 1)[1]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+.-]*:[A-Fa-f0-9]+", digest):
            return "image target must not contain embedded registry credentials"
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return "image target must not contain control characters"
    return None


def new_artifact_paths(
    workspace: Path,
    *,
    family: str,
    prefix: str,
    call_id: Optional[str],
    report_suffix: str,
) -> tuple[Path, Path]:
    """Create collision-resistant report and log paths inside the workspace."""

    root = (workspace / "artifacts" / family).resolve(strict=False)
    try:
        root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("artifact directory resolves outside the workspace") from exc
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    try:
        root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("artifact directory resolves outside the workspace") from exc

    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", call_id or "scan")[:60] or "scan"
    report_fd, report_name = tempfile.mkstemp(
        prefix=f"{prefix}-{safe_id}-",
        suffix=report_suffix,
        dir=str(root),
    )
    os.close(report_fd)
    log_fd, log_name = tempfile.mkstemp(
        prefix=f"{prefix}-{safe_id}-",
        suffix=".log",
        dir=str(root),
    )
    os.close(log_fd)
    return Path(report_name), Path(log_name)


def load_json_report(path: Path) -> Dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("scanner did not produce a JSON report")
        if size > MAX_SUMMARY_REPORT_BYTES:
            raise ValueError(
                "scanner report is too large to summarize safely "
                f"({size} bytes; limit {MAX_SUMMARY_REPORT_BYTES} bytes); "
                f"the complete report remains at {path}"
            )
        with path.open("r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read scanner JSON report: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("scanner JSON report must be an object")
    return report


def append_runtime_log(log_path: Path, runtime: AnchoreRuntimeInfo) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                "$ runtime="
                f"{runtime.source} version={runtime.version or 'unknown'}\n"
            )
    except OSError:
        pass


def append_error_log(log_path: Path, message: str) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(truncate_text(message, MAX_ERROR_CHARS) + "\n")
    except OSError:
        pass


def runtime_payload(runtime: AnchoreRuntimeInfo) -> Dict[str, Any]:
    return {
        "source": runtime.source,
        "version": runtime.version or "unknown",
    }


def truncate_text(value: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    suffix = f"... [{omitted} chars omitted]"
    keep = max(0, max_chars - len(suffix))
    return value[:keep] + suffix


def tail_log(log_path: Path) -> str:
    try:
        with log_path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - MAX_FAILURE_TAIL_CHARS))
            return log_file.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def string_list(value: Any, *, limit: int = 10, max_chars: int = 300) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    return [
        truncate_text(str(item), max_chars)
        for item in values[:limit]
        if item is not None
    ]
