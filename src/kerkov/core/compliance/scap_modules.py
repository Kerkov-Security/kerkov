"""Run-local copies of the Ansible modules used by the pinned SCAP playbook."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import httpx

from kerkov.core.private_files import prepare_private_parent

_MAX_MODULE_BYTES = 128 * 1024


@dataclass(frozen=True)
class ScapModule:
    version: str
    commit: str
    sha256: str


SCAP_MODULES = {
    "community.general.ini_file": ScapModule(
        "13.4.0",
        "049524674b13ad9782849c427266935c8ec61954",
        "00ce10248173a6cf456787fcddf398da470e684c59d6487eed004b3e239195f6",
    ),
    "ansible.posix.sysctl": ScapModule(
        "2.1.0",
        "e54b8bf68f583f2324861a8cb113d4f1bb20f3de",
        "84eaec19f1a9eb8dfcc215ca161cd35e3e03a7c9dd8445c0bc5cb2a0a8142d85",
    ),
}


def _relative_path(name: str) -> Path:
    namespace, collection, module = name.split(".")
    return Path(
        "collections",
        "ansible_collections",
        namespace,
        collection,
        "plugins",
        "modules",
        f"{module}.py",
    )


def _read_verified(path: Path, sha256: str) -> bytes | None:
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as handle:
        data = handle.read(_MAX_MODULE_BYTES + 1)
    if len(data) <= _MAX_MODULE_BYTES and hashlib.sha256(data).hexdigest() == sha256:
        return data
    return None


async def _download_module(
    client: httpx.AsyncClient, name: str, source: ScapModule
) -> bytes:
    collection, _, module = name.rpartition(".")
    url = (
        f"https://raw.githubusercontent.com/ansible-collections/{collection}/"
        f"{source.commit}/plugins/modules/{module}.py"
    )
    data = bytearray()
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                data.extend(chunk)
                if len(data) > _MAX_MODULE_BYTES:
                    raise ValueError(
                        f"The pinned Ansible module {name} exceeds the size limit"
                    )
    except httpx.HTTPError as exc:
        raise ValueError(
            f"Could not download the pinned Ansible module {name}. Check access to GitHub and preview again."
        ) from exc
    if hashlib.sha256(data).hexdigest() != source.sha256:
        raise ValueError(
            f"The pinned Ansible module {name} failed SHA-256 verification"
        )
    return bytes(data)


def _write_cached_module(path: Path, data: bytes) -> None:
    prepare_private_parent(path.parent, managed_parent=path.parent)
    if os.name != "nt" and path.parent.stat().st_uid != os.geteuid():
        raise ValueError("The Ansible module cache must be owned by you")
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_bytes(data)
        temporary.replace(path)


def _stage_modules(directory: Path, content: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        stage = Path(temporary)
        for name, data in content.items():
            target = stage / _relative_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        verify_scap_modules(stage, content)
        (stage / "collections").rename(directory / "collections")


async def stage_scap_modules(
    cache_root: Path, directory: Path, names: Collection[str]
) -> None:
    """Verify cached sources or fetch their pinned bytes, then stage local modules."""
    if not names:
        return
    cache = cache_root.expanduser().resolve() / "scap" / "remediation-modules"
    if cache.resolve() != cache:
        raise ValueError("The Ansible module cache must not use symlinks")
    content: dict[str, bytes] = {}
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for name in names:
            source = SCAP_MODULES[name]
            cached = cache / f"{source.sha256}.py"
            data = await asyncio.to_thread(_read_verified, cached, source.sha256)
            if data is None:
                data = await _download_module(client, name, source)
                await asyncio.to_thread(_write_cached_module, cached, data)
            content[name] = data
    await asyncio.to_thread(_stage_modules, directory, content)


def verify_scap_modules(directory: Path, names: Collection[str]) -> None:
    """Reject changing a reviewed module or adding files under its collection path."""
    modules = {_relative_path(name): SCAP_MODULES[name] for name in names}
    directories = {
        parent for path in modules for parent in path.parents if parent != Path(".")
    }
    root = directory / "collections"
    entries = (
        {item.relative_to(directory) for item in root.rglob("*")}
        | {Path("collections")}
        if root.exists() or root.is_symlink()
        else set()
    )
    if entries != modules.keys() | directories:
        raise ValueError("The reviewed Ansible module directory changed")
    for path in directories:
        candidate = directory / path
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("The reviewed Ansible module directory changed")
    for path, source in modules.items():
        if _read_verified(directory / path, source.sha256) is None:
            raise ValueError("A reviewed Ansible module changed")
