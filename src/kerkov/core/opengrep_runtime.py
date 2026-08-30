"""Locate or explicitly install the latest stable OpenGrep release for Kerkov."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import re
import shutil

# OpenGrep is executed as a fixed argument vector without a shell.
import subprocess  # nosec B404
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from kerkov.core.credentials import sanitized_subprocess_environment
from kerkov.core.process.kill_tree import kill_process_tree

LATEST_RELEASE_API = "https://api.github.com/repos/opengrep/opengrep/releases/latest"
DEFAULT_RUNTIME_TIMEOUT_SECONDS = 180.0
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_BINARY_BYTES = 100 * 1024 * 1024
MAX_BLOCKING_IO_SECONDS = 5.0
PROBE_POLL_SECONDS = 0.05
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")

# Official self-contained binary names for each supported release platform.
OPENGREP_ASSET_NAMES: Mapping[str, str] = {
    "linux-x86_64-glibc": "opengrep_manylinux_x86",
    "linux-aarch64-glibc": "opengrep_manylinux_aarch64",
    "linux-x86_64-musl": "opengrep_musllinux_x86",
    "linux-aarch64-musl": "opengrep_musllinux_aarch64",
    "darwin-x86_64": "opengrep_osx_x86",
    "darwin-arm64": "opengrep_osx_arm64",
    "windows-x86_64": "opengrep_windows_x86.exe",
}


class OpenGrepRuntimeError(RuntimeError):
    """A safe, user-facing failure while preparing OpenGrep."""


class _OpenGrepInstallCancelled(RuntimeError):
    """Internal signal used to stop the synchronous installer worker."""


@dataclass(frozen=True)
class OpenGrepReleaseAsset:
    """Integrity metadata for an official GitHub release binary."""

    version: str
    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class OpenGrepRuntimeInfo:
    """An existing or newly installed OpenGrep executable."""

    binary: str
    source: str
    version: Optional[str]
    installed: bool = False


ReleaseLoader = Callable[[str, float, threading.Event], OpenGrepReleaseAsset]
Downloader = Callable[[OpenGrepReleaseAsset, Path, float, threading.Event], None]
BinaryProbe = Callable[[Path, float, threading.Event], bool]
BinaryLocator = Callable[[str], Optional[str]]


def managed_opengrep_binary_path(
    *,
    install_root: Optional[Path] = None,
    platform_key: Optional[str] = None,
) -> Path:
    """Return the fixed path used by ``kerkov setup opengrep``."""

    root = (install_root or Path.home() / ".kerkov" / "tools" / "opengrep").expanduser()
    resolved_platform = platform_key or current_platform_key()
    filename = (
        "opengrep.exe" if resolved_platform.startswith("windows-") else "opengrep"
    )
    return root / filename


def locate_opengrep(
    *,
    install_root: Optional[Path] = None,
    platform_key: Optional[str] = None,
    locator: BinaryLocator = shutil.which,
) -> Optional[OpenGrepRuntimeInfo]:
    """Locate an existing OpenGrep without downloading or executing anything."""

    located = locator("opengrep")
    if located:
        system_binary = Path(located).expanduser().resolve()
        if system_binary.is_file():
            return OpenGrepRuntimeInfo(
                binary=str(system_binary),
                source="system",
                version=None,
            )

    managed_root = (
        install_root or Path.home() / ".kerkov" / "tools" / "opengrep"
    ).expanduser()
    if managed_root.is_symlink():
        return None
    managed_binary = managed_opengrep_binary_path(
        install_root=managed_root,
        platform_key=platform_key,
    )
    if not managed_binary.is_symlink() and managed_binary.is_file():
        return OpenGrepRuntimeInfo(
            binary=str(managed_binary),
            source="kerkov_install",
            version=None,
        )
    return None


def _remaining_seconds(
    deadline: float,
    cancel_event: threading.Event,
) -> float:
    if cancel_event.is_set():
        raise _OpenGrepInstallCancelled("OpenGrep installation was cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OpenGrepRuntimeError("OpenGrep runtime setup timed out")
    return remaining


def _normalized_machine(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized or "unknown"


def current_platform_key() -> str:
    """Return the release-asset key for the current supported platform."""

    system = platform.system().strip().lower()
    machine = _normalized_machine(platform.machine())
    if system == "linux":
        if machine == "arm64":
            machine = "aarch64"
        libc_name = platform.libc_ver()[0].strip().lower()
        libc_family = (
            "musl"
            if "musl" in libc_name or Path("/etc/alpine-release").is_file()
            else "glibc"
        )
        return f"linux-{machine}-{libc_family}"
    if system == "darwin":
        return f"darwin-{machine}"
    if system == "windows":
        return f"windows-{machine}"
    return f"{system or 'unknown'}-{machine}"


def _request(url: str, *, accept: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Kerkov OpenGrep installer",
        },
    )


def _set_response_timeout(response: object, timeout: float) -> None:
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(timeout)


def _read_response_chunk(response: object, max_bytes: int) -> bytes:
    read1 = getattr(response, "read1", None)
    if not callable(read1):
        raise OpenGrepRuntimeError("the release server returned an invalid response")
    chunk = read1(max_bytes)
    if not isinstance(chunk, bytes):
        raise OpenGrepRuntimeError("the release server returned invalid metadata")
    return chunk


def _read_limited(
    response: object,
    max_bytes: int,
    deadline: float,
    cancel_event: threading.Event,
) -> bytes:
    payload = bytearray()
    while True:
        remaining = _remaining_seconds(deadline, cancel_event)
        _set_response_timeout(
            response,
            min(MAX_BLOCKING_IO_SECONDS, remaining),
        )
        chunk = _read_response_chunk(
            response,
            min(DOWNLOAD_CHUNK_BYTES, max_bytes + 1 - len(payload)),
        )
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise OpenGrepRuntimeError("the release metadata exceeded its safety limit")
    return bytes(payload)


def _latest_release_asset(
    platform_key: str,
    deadline: float,
    cancel_event: threading.Event,
) -> OpenGrepReleaseAsset:
    expected_name = OPENGREP_ASSET_NAMES.get(platform_key)
    if expected_name is None:
        supported = ", ".join(sorted(OPENGREP_ASSET_NAMES))
        raise OpenGrepRuntimeError(
            f"automatic OpenGrep installation does not support {platform_key!r}; "
            f"supported platforms: {supported}"
        )

    request = _request(
        LATEST_RELEASE_API,
        accept="application/vnd.github+json",
    )
    try:
        timeout = min(
            MAX_BLOCKING_IO_SECONDS,
            _remaining_seconds(deadline, cancel_event),
        )
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            payload = _read_limited(
                response,
                MAX_RELEASE_METADATA_BYTES,
                deadline,
                cancel_event,
            )
        _remaining_seconds(deadline, cancel_event)
        release = json.loads(payload.decode("utf-8"))
    except OpenGrepRuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenGrepRuntimeError(
            f"could not read OpenGrep's latest official release: {exc}"
        ) from exc
    if not isinstance(release, dict):
        raise OpenGrepRuntimeError("the latest OpenGrep release metadata was invalid")
    if release.get("draft") is True or release.get("prerelease") is True:
        raise OpenGrepRuntimeError("GitHub returned a non-stable OpenGrep release")

    version = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version.strip()):
        raise OpenGrepRuntimeError("the latest OpenGrep release had no valid version")
    if not isinstance(assets, list):
        raise OpenGrepRuntimeError("the latest OpenGrep release had no assets")

    raw_asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == expected_name
        ),
        None,
    )
    if raw_asset is None:
        raise OpenGrepRuntimeError(
            f"OpenGrep {version} has no binary for {platform_key}"
        )
    size = raw_asset.get("size")
    url = raw_asset.get("browser_download_url")
    raw_digest = raw_asset.get("digest")
    if not isinstance(size, int) or not 0 < size <= MAX_BINARY_BYTES:
        raise OpenGrepRuntimeError("the OpenGrep release asset size was invalid")
    if not isinstance(url, str) or not _is_official_download_url(url):
        raise OpenGrepRuntimeError("the OpenGrep release asset URL was invalid")
    if not isinstance(raw_digest, str) or not raw_digest.startswith("sha256:"):
        raise OpenGrepRuntimeError(
            "the latest OpenGrep release did not publish a SHA-256 digest"
        )
    digest = raw_digest.removeprefix("sha256:").lower()
    if not _SHA256_RE.fullmatch(digest):
        raise OpenGrepRuntimeError("the OpenGrep release SHA-256 digest was invalid")
    return OpenGrepReleaseAsset(
        version=version.strip(),
        filename=expected_name,
        url=url,
        size=size,
        sha256=digest,
    )


def _is_official_download_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith("/opengrep/opengrep/releases/download/")
    )


def _sha256(
    path: Path,
    deadline: float,
    cancel_event: threading.Event,
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _remaining_seconds(deadline, cancel_event)
            chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    _remaining_seconds(deadline, cancel_event)
    return digest.hexdigest()


def _download_asset(
    asset: OpenGrepReleaseAsset,
    destination: Path,
    deadline: float,
    cancel_event: threading.Event,
) -> None:
    request = _request(asset.url, accept="application/octet-stream")
    written = 0
    try:
        timeout = min(
            MAX_BLOCKING_IO_SECONDS,
            _remaining_seconds(deadline, cancel_event),
        )
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            with destination.open("wb") as output:
                while True:
                    remaining = _remaining_seconds(deadline, cancel_event)
                    _set_response_timeout(
                        response,
                        min(MAX_BLOCKING_IO_SECONDS, remaining),
                    )
                    chunk = _read_response_chunk(response, DOWNLOAD_CHUNK_BYTES)
                    _remaining_seconds(deadline, cancel_event)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > asset.size:
                        raise OpenGrepRuntimeError(
                            "the OpenGrep download exceeded its published size"
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except OpenGrepRuntimeError:
        raise
    except OSError as exc:
        raise OpenGrepRuntimeError(f"could not download OpenGrep: {exc}") from exc
    if written != asset.size:
        raise OpenGrepRuntimeError(
            "the OpenGrep download size did not match the published release"
        )


def _stop_probe(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    kill_process_tree(process.pid, grace_ms=0)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _probe_binary(
    binary: Path,
    deadline: float,
    cancel_event: threading.Event,
) -> bool:
    _remaining_seconds(deadline, cancel_event)
    try:
        process = subprocess.Popen(  # nosec B603
            [str(binary), "--version"],
            cwd=str(Path.cwd()),
            env=sanitized_subprocess_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        while process.poll() is None:
            remaining = _remaining_seconds(deadline, cancel_event)
            cancel_event.wait(min(PROBE_POLL_SECONDS, remaining))
        return process.returncode == 0
    except (_OpenGrepInstallCancelled, OpenGrepRuntimeError):
        _stop_probe(process)
        raise


class OpenGrepInstaller:
    """Use a working OpenGrep or install the latest stable release once."""

    def __init__(
        self,
        *,
        install_root: Optional[Path] = None,
        platform_key: Optional[str] = None,
        release_loader: ReleaseLoader = _latest_release_asset,
        downloader: Downloader = _download_asset,
        probe: BinaryProbe = _probe_binary,
        locator: BinaryLocator = shutil.which,
    ) -> None:
        self.install_root = (
            install_root or Path.home() / ".kerkov" / "tools" / "opengrep"
        ).expanduser()
        self.platform_key = platform_key or current_platform_key()
        self._release_loader = release_loader
        self._downloader = downloader
        self._probe = probe
        self._locator = locator
        self._lock = asyncio.Lock()

    async def setup(
        self,
        *,
        timeout_seconds: float = DEFAULT_RUNTIME_TIMEOUT_SECONDS,
    ) -> OpenGrepRuntimeInfo:
        """Select an executable, explicitly installing latest stable if needed."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise OpenGrepRuntimeError("OpenGrep runtime setup timed out")
        async with self._lock:
            cancel_event = threading.Event()
            deadline = time.monotonic() + timeout_seconds
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._resolve_sync,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel_event.set()
                with suppress(Exception):
                    await asyncio.shield(worker)
                raise

    def _resolve_sync(
        self,
        *,
        deadline: float,
        cancel_event: threading.Event,
    ) -> OpenGrepRuntimeInfo:
        _remaining_seconds(deadline, cancel_event)
        located = self._locator("opengrep")
        if located:
            system_binary = Path(located).expanduser().resolve()
            if system_binary.is_file() and self._probe(
                system_binary, deadline, cancel_event
            ):
                return OpenGrepRuntimeInfo(
                    binary=str(system_binary),
                    source="system",
                    version=None,
                )

        if self.install_root.is_symlink():
            raise OpenGrepRuntimeError(
                "Kerkov's OpenGrep installation directory must not be a symlink"
            )
        installed_binary = self._installed_binary_path()
        if (
            not installed_binary.is_symlink()
            and installed_binary.is_file()
            and self._probe(installed_binary, deadline, cancel_event)
        ):
            return OpenGrepRuntimeInfo(
                binary=str(installed_binary),
                source="kerkov_install",
                version=None,
            )
        asset = self._release_loader(self.platform_key, deadline, cancel_event)
        self._install(installed_binary, asset, deadline, cancel_event)
        return OpenGrepRuntimeInfo(
            binary=str(installed_binary),
            source="kerkov_install",
            version=asset.version,
            installed=True,
        )

    def _installed_binary_path(self) -> Path:
        return managed_opengrep_binary_path(
            install_root=self.install_root,
            platform_key=self.platform_key,
        )

    def _install(
        self,
        binary: Path,
        asset: OpenGrepReleaseAsset,
        deadline: float,
        cancel_event: threading.Event,
    ) -> None:
        _remaining_seconds(deadline, cancel_event)
        try:
            if self.install_root.is_symlink():
                raise OpenGrepRuntimeError(
                    "Kerkov's OpenGrep installation directory must not be a symlink"
                )
            self.install_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.install_root.is_symlink() or not self.install_root.is_dir():
                raise OpenGrepRuntimeError(
                    "Kerkov's OpenGrep installation path is not a safe directory"
                )
            if os.name != "nt":
                self.install_root.chmod(0o700)
            suffix = ".exe" if self.platform_key.startswith("windows-") else ".tmp"
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=".opengrep-download-",
                suffix=suffix,
                dir=str(self.install_root),
            )
            os.close(temp_fd)
        except OpenGrepRuntimeError:
            raise
        except OSError as exc:
            raise OpenGrepRuntimeError(
                "could not create Kerkov's user-local OpenGrep installation: " f"{exc}"
            ) from exc

        temporary = Path(temp_name)
        try:
            self._downloader(asset, temporary, deadline, cancel_event)
            _remaining_seconds(deadline, cancel_event)
            if temporary.stat().st_size != asset.size:
                raise OpenGrepRuntimeError(
                    "the OpenGrep download size did not match the published release"
                )
            if _sha256(temporary, deadline, cancel_event) != asset.sha256:
                raise OpenGrepRuntimeError(
                    "the OpenGrep download failed SHA-256 verification"
                )
            if os.name != "nt":
                temporary.chmod(0o755)
            if not self._probe(temporary, deadline, cancel_event):
                raise OpenGrepRuntimeError(
                    "the verified OpenGrep binary could not run on this system"
                )
            _remaining_seconds(deadline, cancel_event)
            os.replace(temporary, binary)
        except (_OpenGrepInstallCancelled, OpenGrepRuntimeError):
            raise
        except (OSError, ValueError) as exc:
            raise OpenGrepRuntimeError(f"could not install OpenGrep: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
