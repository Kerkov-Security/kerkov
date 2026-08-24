"""Resolve or install official Syft and Grype release binaries for Kerkov."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil

# Syft and Grype are executed as fixed argument vectors without a shell.
import subprocess  # nosec B404
import tarfile
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from kerkov.core.credentials import sanitized_subprocess_environment

SUPPORTED_TOOLS = frozenset({"syft", "grype"})
DOWNLOAD_TIMEOUT_SECONDS = 180.0
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
MAX_BINARY_BYTES = 250 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")


class AnchoreRuntimeError(RuntimeError):
    """A safe, user-facing failure while preparing an Anchore tool."""


@dataclass(frozen=True)
class AnchoreReleaseAsset:
    """Validated metadata for an official Anchore release archive."""

    tool: str
    version: str
    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class AnchoreRuntimeInfo:
    """The executable selected for a Syft or Grype operation."""

    binary: str
    source: str
    version: Optional[str]
    installed: bool = False


class AnchoreRuntimeResolver(Protocol):
    """Injectable runtime resolver used by the supply-chain tools."""

    async def resolve(
        self, *, install_if_missing: bool, force_install: bool = False
    ) -> AnchoreRuntimeInfo:
        """Return a usable executable."""
        ...


ReleaseLoader = Callable[[str, str], AnchoreReleaseAsset]
Downloader = Callable[[AnchoreReleaseAsset, Path, float], None]
BinaryProbe = Callable[[Path], bool]
BinaryLocator = Callable[[str], Optional[str]]


def _normalize_machine(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"amd64", "x64", "x86_64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized or "unknown"


def current_platform_key() -> str:
    """Return the platform fragment used in official Anchore archives."""

    system = platform.system().strip().lower()
    machine = _normalize_machine(platform.machine())
    if system == "windows":
        return f"windows-{machine}"
    if system in {"linux", "darwin"}:
        return f"{system}-{machine}"
    return f"{system or 'unknown'}-{machine}"


def _request(url: str, *, accept: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Kerkov Anchore tools installer",
        },
    )


def _read_limited(response: object, max_bytes: int) -> bytes:
    read = getattr(response, "read", None)
    if not callable(read):
        raise AnchoreRuntimeError("the release server returned an invalid response")
    payload = read(max_bytes + 1)
    if not isinstance(payload, bytes):
        raise AnchoreRuntimeError("the release server returned invalid data")
    if len(payload) > max_bytes:
        raise AnchoreRuntimeError("the release response exceeded its safety limit")
    return payload


def _release_api(tool: str) -> str:
    return f"https://api.github.com/repos/anchore/{tool}/releases/latest"


def _archive_name(tool: str, version: str, platform_key: str) -> str:
    try:
        system, machine = platform_key.split("-", 1)
    except ValueError as exc:
        raise AnchoreRuntimeError(
            f"automatic {tool} installation does not support {platform_key!r}"
        ) from exc
    if system not in {"linux", "darwin", "windows"} or machine not in {
        "amd64",
        "arm64",
    }:
        raise AnchoreRuntimeError(
            f"automatic {tool} installation does not support {platform_key!r}; "
            "supported systems are Linux, macOS, and Windows on amd64 or arm64"
        )
    extension = "zip" if system == "windows" else "tar.gz"
    return f"{tool}_{version.removeprefix('v')}_{system}_{machine}.{extension}"


def _is_official_download_url(url: str, tool: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(f"/anchore/{tool}/releases/download/")
    )


def _asset_by_name(assets: object, name: str) -> dict[str, object]:
    if not isinstance(assets, list):
        raise AnchoreRuntimeError("the latest release had no assets")
    selected = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == name
        ),
        None,
    )
    if selected is None:
        raise AnchoreRuntimeError(f"the latest release did not include {name}")
    return selected


def _validated_asset_fields(
    asset: dict[str, object],
    *,
    tool: str,
    label: str,
    max_size: int,
) -> tuple[str, int]:
    url = asset.get("browser_download_url")
    size = asset.get("size")
    if not isinstance(url, str) or not _is_official_download_url(url, tool):
        raise AnchoreRuntimeError(f"the {tool} {label} URL was invalid")
    if not isinstance(size, int) or not 0 < size <= max_size:
        raise AnchoreRuntimeError(f"the {tool} {label} size was invalid")
    return url, size


def _download_small(url: str, *, expected_size: int, max_bytes: int) -> bytes:
    try:
        # The URL was constrained to an official Anchore GitHub release above.
        with urlopen(  # nosec B310
            _request(url, accept="application/octet-stream"), timeout=30
        ) as response:
            payload = _read_limited(response, max_bytes)
    except AnchoreRuntimeError:
        raise
    except (OSError, HTTPException) as exc:
        raise AnchoreRuntimeError(
            f"could not download release checksums: {exc}"
        ) from exc
    if len(payload) != expected_size:
        raise AnchoreRuntimeError(
            "the checksum download size did not match the published release"
        )
    return payload


def _checksum_for(checksums: bytes, filename: str) -> str:
    try:
        text = checksums.decode("utf-8")
    except UnicodeError as exc:
        raise AnchoreRuntimeError("the release checksums were not valid UTF-8") from exc
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9A-Fa-f]{64})\s+\*?(.+)", line.strip())
        if match and match.group(2) == filename:
            digest = match.group(1).lower()
            if _SHA256_RE.fullmatch(digest):
                return digest
    raise AnchoreRuntimeError(
        f"the release checksums did not include an SHA-256 for {filename}"
    )


def _latest_release_asset(tool: str, platform_key: str) -> AnchoreReleaseAsset:
    if tool not in SUPPORTED_TOOLS:
        raise AnchoreRuntimeError(f"unsupported Anchore tool: {tool!r}")
    try:
        # This is a fixed official GitHub API URL.
        with urlopen(  # nosec B310
            _request(_release_api(tool), accept="application/vnd.github+json"),
            timeout=30,
        ) as response:
            payload = _read_limited(response, MAX_RELEASE_METADATA_BYTES)
        release = json.loads(payload.decode("utf-8"))
    except AnchoreRuntimeError:
        raise
    except (OSError, HTTPException, UnicodeError, json.JSONDecodeError) as exc:
        raise AnchoreRuntimeError(
            f"could not read {tool}'s latest official release: {exc}"
        ) from exc
    if not isinstance(release, dict):
        raise AnchoreRuntimeError(f"the latest {tool} release metadata was invalid")
    if release.get("draft") is True or release.get("prerelease") is True:
        raise AnchoreRuntimeError(f"GitHub returned a non-stable {tool} release")

    raw_version = release.get("tag_name")
    if not isinstance(raw_version, str) or not _VERSION_RE.fullmatch(
        raw_version.strip()
    ):
        raise AnchoreRuntimeError(f"the latest {tool} release had no valid version")
    version = raw_version.strip()
    archive_name = _archive_name(tool, version, platform_key)
    checksum_name = f"{tool}_{version.removeprefix('v')}_checksums.txt"
    archive = _asset_by_name(release.get("assets"), archive_name)
    checksum_asset = _asset_by_name(release.get("assets"), checksum_name)
    archive_url, archive_size = _validated_asset_fields(
        archive,
        tool=tool,
        label="archive",
        max_size=MAX_ARCHIVE_BYTES,
    )
    raw_digest = archive.get("digest")
    if not isinstance(raw_digest, str) or not raw_digest.startswith("sha256:"):
        raise AnchoreRuntimeError(
            f"the latest {tool} release did not publish an archive SHA-256"
        )
    release_digest = raw_digest.removeprefix("sha256:").lower()
    if not _SHA256_RE.fullmatch(release_digest):
        raise AnchoreRuntimeError(f"the {tool} archive SHA-256 was invalid")
    checksum_url, checksum_size = _validated_asset_fields(
        checksum_asset,
        tool=tool,
        label="checksum file",
        max_size=MAX_CHECKSUM_BYTES,
    )
    checksums = _download_small(
        checksum_url,
        expected_size=checksum_size,
        max_bytes=MAX_CHECKSUM_BYTES,
    )
    checksum_digest = _checksum_for(checksums, archive_name)
    if checksum_digest != release_digest:
        raise AnchoreRuntimeError(
            f"the {tool} release digest did not match its checksum file"
        )
    return AnchoreReleaseAsset(
        tool=tool,
        version=version,
        filename=archive_name,
        url=archive_url,
        size=archive_size,
        sha256=release_digest,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_asset(
    asset: AnchoreReleaseAsset,
    destination: Path,
    timeout_seconds: float,
) -> None:
    written = 0
    try:
        # The release URL was constrained to Anchore's GitHub organization.
        with urlopen(  # nosec B310
            _request(asset.url, accept="application/octet-stream"),
            timeout=timeout_seconds,
        ) as response:
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > asset.size:
                        raise AnchoreRuntimeError(
                            f"the {asset.tool} download exceeded its published size"
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except AnchoreRuntimeError:
        raise
    except (OSError, HTTPException) as exc:
        raise AnchoreRuntimeError(f"could not download {asset.tool}: {exc}") from exc
    if written != asset.size:
        raise AnchoreRuntimeError(
            f"the {asset.tool} download size did not match the published release"
        )


def _copy_bounded(source: object, destination: Path, expected_size: int) -> None:
    read = getattr(source, "read", None)
    if not callable(read):
        raise AnchoreRuntimeError("the release archive contained an invalid binary")
    if not 0 < expected_size <= MAX_BINARY_BYTES:
        raise AnchoreRuntimeError("the release binary size was invalid")
    written = 0
    with destination.open("wb") as output:
        while True:
            chunk = read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise AnchoreRuntimeError("the release binary data was invalid")
            written += len(chunk)
            if written > expected_size or written > MAX_BINARY_BYTES:
                raise AnchoreRuntimeError("the extracted release binary was too large")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if written != expected_size:
        raise AnchoreRuntimeError("the extracted release binary size was invalid")


def _extract_binary(archive: Path, destination: Path, tool: str) -> None:
    executable = f"{tool}.exe" if archive.suffix == ".zip" else tool
    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                zip_matches = [
                    item
                    for item in bundle.infolist()
                    if not item.is_dir()
                    and PurePosixPath(item.filename).name == executable
                ]
                if len(zip_matches) != 1:
                    raise AnchoreRuntimeError(
                        f"the {tool} archive did not contain exactly one executable"
                    )
                with bundle.open(zip_matches[0]) as zip_source:
                    _copy_bounded(zip_source, destination, zip_matches[0].file_size)
            return

        with tarfile.open(archive, mode="r:gz") as bundle:
            tar_matches = [
                item
                for item in bundle.getmembers()
                if item.isfile() and PurePosixPath(item.name).name == executable
            ]
            if len(tar_matches) != 1:
                raise AnchoreRuntimeError(
                    f"the {tool} archive did not contain exactly one executable"
                )
            tar_source = bundle.extractfile(tar_matches[0])
            if tar_source is None:
                raise AnchoreRuntimeError(
                    f"the {tool} executable could not be read from its archive"
                )
            with tar_source:
                _copy_bounded(tar_source, destination, tar_matches[0].size)
    except AnchoreRuntimeError:
        raise
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise AnchoreRuntimeError(f"could not extract {tool}: {exc}") from exc


def _probe_binary(binary: Path) -> bool:
    try:
        result = subprocess.run(  # nosec B603
            [str(binary), "version"],
            cwd=str(Path.cwd()),
            env=sanitized_subprocess_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class AnchoreInstaller:
    """Use a working Anchore CLI or install its latest stable release."""

    def __init__(
        self,
        tool: str,
        *,
        install_root: Optional[Path] = None,
        platform_key: Optional[str] = None,
        release_loader: ReleaseLoader = _latest_release_asset,
        downloader: Downloader = _download_asset,
        probe: BinaryProbe = _probe_binary,
        locator: BinaryLocator = shutil.which,
    ) -> None:
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(f"unsupported Anchore tool: {tool!r}")
        self.tool = tool
        self.install_root = (
            install_root or Path.home() / ".kerkov" / "tools" / tool
        ).expanduser()
        self.platform_key = platform_key or current_platform_key()
        self._release_loader = release_loader
        self._downloader = downloader
        self._probe = probe
        self._locator = locator
        self._lock = threading.Lock()

    async def resolve(
        self, *, install_if_missing: bool, force_install: bool = False
    ) -> AnchoreRuntimeInfo:
        """Select an executable, optionally forcing a managed installation."""

        return await asyncio.to_thread(
            self._resolve_locked,
            install_if_missing=install_if_missing,
            force_install=force_install,
        )

    def _resolve_locked(
        self, *, install_if_missing: bool, force_install: bool
    ) -> AnchoreRuntimeInfo:
        with self._lock:
            return self._resolve_sync(
                install_if_missing=install_if_missing,
                force_install=force_install,
            )

    def _resolve_sync(
        self, *, install_if_missing: bool, force_install: bool
    ) -> AnchoreRuntimeInfo:
        if force_install and not install_if_missing:
            raise AnchoreRuntimeError("force_install requires install_if_missing=true")
        if not force_install:
            located = self._locator(self.tool)
            if located:
                system_binary = Path(located).expanduser().resolve()
                if system_binary.is_file() and self._probe(system_binary):
                    return AnchoreRuntimeInfo(
                        binary=str(system_binary),
                        source="system",
                        version=None,
                    )

            installed_binary = self._installed_binary_path()
            if (
                not installed_binary.is_symlink()
                and installed_binary.is_file()
                and self._probe(installed_binary)
            ):
                return AnchoreRuntimeInfo(
                    binary=str(installed_binary),
                    source="kerkov_install",
                    version=None,
                )
        if not install_if_missing:
            raise AnchoreRuntimeError(
                f"no usable {self.tool} binary was found. Set "
                "install_if_missing=true to let Kerkov install the latest "
                f"stable release, or install {self.tool} on PATH"
            )

        installed_binary = self._installed_binary_path()
        asset = self._release_loader(self.tool, self.platform_key)
        self._install(installed_binary, asset)
        return AnchoreRuntimeInfo(
            binary=str(installed_binary),
            source="kerkov_install",
            version=asset.version,
            installed=True,
        )

    def _installed_binary_path(self) -> Path:
        filename = (
            f"{self.tool}.exe"
            if self.platform_key.startswith("windows-")
            else self.tool
        )
        return self.install_root / filename

    def _install(self, binary: Path, asset: AnchoreReleaseAsset) -> None:
        try:
            if self.install_root.is_symlink():
                raise AnchoreRuntimeError(
                    f"Kerkov's {self.tool} installation directory must not be a symlink"
                )
            self.install_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.install_root.is_symlink() or not self.install_root.is_dir():
                raise AnchoreRuntimeError(
                    f"Kerkov's {self.tool} installation path is not a safe directory"
                )
            if os.name != "nt":
                self.install_root.chmod(0o700)
            archive_fd, archive_name = tempfile.mkstemp(
                prefix=f".{self.tool}-download-",
                suffix=".zip" if asset.filename.endswith(".zip") else ".tar.gz",
                dir=str(self.install_root),
            )
            os.close(archive_fd)
            binary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.tool}-binary-",
                suffix=".exe" if self.platform_key.startswith("windows-") else ".tmp",
                dir=str(self.install_root),
            )
            os.close(binary_fd)
        except AnchoreRuntimeError:
            raise
        except OSError as exc:
            raise AnchoreRuntimeError(
                f"could not create Kerkov's user-local {self.tool} installation: {exc}"
            ) from exc

        archive = Path(archive_name)
        temporary = Path(temporary_name)
        try:
            self._downloader(asset, archive, DOWNLOAD_TIMEOUT_SECONDS)
            if archive.stat().st_size != asset.size:
                raise AnchoreRuntimeError(
                    f"the {self.tool} download size did not match the published release"
                )
            if _sha256(archive) != asset.sha256:
                raise AnchoreRuntimeError(
                    f"the {self.tool} download failed SHA-256 verification"
                )
            _extract_binary(archive, temporary, self.tool)
            if os.name != "nt":
                temporary.chmod(0o755)
            if not self._probe(temporary):
                raise AnchoreRuntimeError(
                    f"the verified {self.tool} binary could not run on this system"
                )
            os.replace(temporary, binary)
        except AnchoreRuntimeError:
            raise
        except (OSError, ValueError) as exc:
            raise AnchoreRuntimeError(f"could not install {self.tool}: {exc}") from exc
        finally:
            for path in (archive, temporary):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
