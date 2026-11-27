"""Pinned SCAP benchmarks and their verified local cache."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiofiles
import httpx

from kerkov.core.compliance.profile_sources import cache_generation_lock
from kerkov.core.compliance.reports import MAX_REPORT_BYTES
from kerkov.core.private_files import prepare_private_parent

_RELEASE = "0.1.82"
_RELEASE_NAME = f"scap-security-guide-{_RELEASE}"
_REPOSITORY = "https://github.com/ComplianceAsCode/content/releases"
_RELEASE_URL = f"{_REPOSITORY}/tag/v{_RELEASE}"
_ARCHIVE_URL = f"{_REPOSITORY}/download/v{_RELEASE}/{_RELEASE_NAME}.zip"
_ARCHIVE_SHA256 = "765e84bdce7f9055f9b9c2dd0ee2b713d4255f8eec94eac6d35ea4973c28919c"
_MAX_ARCHIVE_BYTES = 190 * 1024 * 1024
_PCI_PROFILE = "xccdf_org.ssgproject.content_profile_pci-dss"


@dataclass(frozen=True)
class ScapPreset:
    major: int
    sha256: str

    @property
    def id(self) -> str:
        return f"pci-dss-rhel{self.major}-ssg-{_RELEASE}"

    @property
    def name(self) -> str:
        return f"PCI DSS v4.0.1 · RHEL {self.major}"

    @property
    def filename(self) -> str:
        return f"ssg-rhel{self.major}-ds.xml"

    def settings(self) -> dict[str, str]:
        return {
            "distribution": "RedHat",
            "version": f"{self.major}.*",
            "profile_id": _PCI_PROFILE,
            "sha256": self.sha256,
        }

    def source(self) -> dict[str, str]:
        return {
            "publisher": "ComplianceAsCode",
            "url": _RELEASE_URL,
            "release": _RELEASE,
            "archive_sha256": _ARCHIVE_SHA256,
            "filename": self.filename,
        }


_PRESETS = {
    preset.id: preset
    for preset in (
        ScapPreset(
            8, "82592d3cff74a82ce652a65ba67cd755cf57550269798d4c449c2069417c1028"
        ),
        ScapPreset(
            9, "92204daafbf4f38011671ef034fae4cffb48f708516186710346a9ec702a1f8f"
        ),
        ScapPreset(
            10, "3a22f1f747ef7b895268b2da838f680b6a7ea421c5132d6ac4141d858afc60c6"
        ),
    )
}


def scap_preset(preset_id: str | None) -> ScapPreset | None:
    return _PRESETS.get(preset_id) if preset_id else None


def scap_catalog() -> list[dict[str, Any]]:
    return [
        {
            **preset.source(),
            "id": preset.id,
            "name": preset.name,
            "platform": f"Red Hat Enterprise Linux {preset.major}",
            "description": "Assess host settings mapped to PCI DSS v4.0.1.",
            "openscap": preset.settings(),
        }
        for preset in _PRESETS.values()
    ]


async def _download_archive(path: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, trust_env=False
        ) as client:
            async with client.stream("GET", _ARCHIVE_URL) as response:
                response.raise_for_status()
                async with aiofiles.open(path, "wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=512 * 1024):
                        size += len(chunk)
                        if size > _MAX_ARCHIVE_BYTES:
                            raise ValueError("SCAP release exceeds the download limit")
                        digest.update(chunk)
                        await handle.write(chunk)
    except httpx.HTTPError as exc:
        raise ValueError(
            "Could not download the SCAP release from GitHub. Check connectivity and retry."
        ) from exc
    if digest.hexdigest() != _ARCHIVE_SHA256:
        raise ValueError("Downloaded SCAP release does not match the reviewed SHA-256")


_FIX_FILES = {
    8: "41f1910c702786ae9939d32768d19af8f0ac21ff5b28ad9044bc274252c68885",
    9: "e221f28c61516997eb6aeb0bf55dac086ae2aaab11065659dacc768557188a46",
    10: "a89e524ff6e6ea363c82b0750a8929d885527b3685848cf1db98b03e3664d111",
}


def scap_playbook_name(major: int) -> str:
    return f"rhel{major}-playbook-pci-dss.yml"


def _release_files() -> dict[str, tuple[str, str]]:
    return {
        **{
            preset.filename: (preset.filename, preset.sha256)
            for preset in _PRESETS.values()
        },
        **{
            scap_playbook_name(major): (f"ansible/{scap_playbook_name(major)}", digest)
            for major, digest in _FIX_FILES.items()
        },
    }


def _extract_release(
    archive: Path, stage: Path, files: dict[str, tuple[str, str]]
) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            for filename, (relative, sha256) in files.items():
                entry = bundle.getinfo(f"{_RELEASE_NAME}/{relative}")
                if not 0 < entry.file_size <= MAX_REPORT_BYTES:
                    raise ValueError("SCAP release file exceeds the processing limit")
                data = bundle.read(entry)
                if hashlib.sha256(data).hexdigest() != sha256:
                    raise ValueError(
                        "SCAP release file does not match the reviewed SHA-256"
                    )
                (stage / filename).write_bytes(data)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("SCAP release archive is incomplete or invalid") from exc


def _cached_match(path: Path, sha256: str) -> bool:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_REPORT_BYTES
    ):
        return False
    with path.open("rb") as handle:
        return hashlib.sha256(handle.read(MAX_REPORT_BYTES + 1)).hexdigest() == sha256


def _scap_cache(root: Path) -> Path:
    directory = root.expanduser().resolve() / "scap" / f"v{_RELEASE}"
    if directory.resolve() != directory:
        raise ValueError("SCAP cache directory must not use symlinks")
    return directory


async def prepare_scap_preset(root: Path, preset: ScapPreset) -> Path:
    return await _prepare_release_file(root, preset.filename, preset.sha256)


async def prepare_scap_playbook(root: Path, major: int) -> Path:
    return await _prepare_release_file(
        root, scap_playbook_name(major), _FIX_FILES[major]
    )


def read_scap_playbook(path: Path, major: int) -> bytes:
    """Read exactly the PCI DSS Ansible content pinned for this release."""
    with path.open("rb") as handle:
        body = handle.read(MAX_REPORT_BYTES + 1)
    if (
        len(body) > MAX_REPORT_BYTES
        or hashlib.sha256(body).hexdigest() != _FIX_FILES[major]
    ):
        raise ValueError("The PCI DSS playbook does not match its pinned SHA-256")
    return body


def cached_scap_playbook(
    root: Path, major: int, *, verify: bool = False
) -> Path | None:
    path = _scap_cache(root) / scap_playbook_name(major)
    if verify:
        return path if _cached_match(path, _FIX_FILES[major]) else None
    return path if path.is_file() and not path.is_symlink() else None


async def _prepare_release_file(root: Path, filename: str, sha256: str) -> Path:
    directory = _scap_cache(root)
    prepare_private_parent(directory, managed_parent=directory)
    if os.name != "nt" and directory.stat().st_uid != os.geteuid():
        raise ValueError("SCAP cache directory must be owned by you")
    destination = directory / filename
    try:
        async with asyncio.timeout(180):
            with ExitStack() as lock:
                while True:
                    try:
                        lock.enter_context(
                            cache_generation_lock(
                                directory, exclusive=True, blocking=False
                            )
                        )
                    except BlockingIOError:
                        await asyncio.sleep(0.25)
                    else:
                        break
                if await asyncio.to_thread(_cached_match, destination, sha256):
                    return destination
                with tempfile.TemporaryDirectory(
                    prefix=".stage-", dir=directory
                ) as name:
                    stage = Path(name)
                    archive = stage / "release.zip"
                    await _download_archive(archive)
                    files = _release_files()
                    extract = asyncio.create_task(
                        asyncio.to_thread(_extract_release, archive, stage, files)
                    )
                    try:
                        await asyncio.shield(extract)
                    except asyncio.CancelledError:
                        await asyncio.gather(extract, return_exceptions=True)
                        raise
                    for name in files:
                        (stage / name).replace(directory / name)
    except TimeoutError as exc:
        raise ValueError(
            "Preparing the SCAP release timed out. Retry when connected."
        ) from exc
    return destination
