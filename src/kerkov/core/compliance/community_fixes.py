"""Curated, source-pinned remediation bundles for operator review."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kerkov.core.automation.profiles import confined_path, file_digest, read_document
from kerkov.core.compliance.cinc_runtime import snapshot_profile
from kerkov.core.compliance.profile_sources import (
    cache_generation_lock,
    profile_catalog,
)
from kerkov.core.compliance.projects import GitHubProfiles, extract_project
from kerkov.core.compliance.scap_catalog import (
    cached_scap_playbook,
    prepare_scap_playbook,
    scap_catalog,
)
from kerkov.core.private_files import prepare_private_parent, write_private_json


@dataclass(frozen=True)
class Release:
    name: str
    url: str
    version: str
    commit: str


@dataclass(frozen=True)
class CommunityFix:
    id: str
    profile_id: str
    name: str
    family: str
    publisher: str
    platform: str
    scope: str
    note: str
    release: Release
    role: str = ""
    rhel_major: int = 0

    def summary(self) -> dict[str, str]:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "name": self.name,
            "family": self.family,
            "publisher": self.publisher,
            "platform": self.platform,
            "scope": self.scope,
            "note": self.note,
            "url": self.release.url,
            "version": self.release.version,
            "revision": self.release.commit,
        }


_DEVSEC = Release(
    "devsec-hardening",
    "https://github.com/dev-sec/ansible-collection-hardening",
    "11.0.0",
    "c60ead8a091cc41ce6d0cf1cd40fcc95027defce",
)
_DEVSEC_FIXES = (
    (
        "devsec-linux",
        "Linux hardening",
        "Supported Linux distributions",
        "os_hardening",
    ),
    (
        "devsec-ssh",
        "OpenSSH hardening",
        "Supported Linux hosts with OpenSSH",
        "ssh_hardening",
    ),
    (
        "devsec-nginx",
        "NGINX hardening",
        "Supported Linux hosts with NGINX",
        "nginx_hardening",
    ),
)
_STIG_FIXES = (
    (
        "mitre-ubuntu-2204-stig",
        "Ubuntu 22.04",
        "UBUNTU22-STIG",
        "2.9.0",
        "812017d433627c9fe41c493d3f93755bb4b19d52",
        "V2R9",
    ),
    (
        "mitre-ubuntu-2404-stig",
        "Ubuntu 24.04",
        "UBUNTU24-STIG",
        "1.6.0",
        "c3176df55eaf3885e9fcdf283a9cf2ae47066e36",
        "V1R6",
    ),
    (
        "mitre-rhel-8-stig",
        "RHEL 8",
        "RHEL8-STIG",
        "4.4.1",
        "bb966381dc1a0a4e32df699ff8a145b32dcbb46f",
        "V2R8",
    ),
    (
        "mitre-rhel-9-stig",
        "RHEL 9",
        "RHEL9-STIG",
        "2.9.0",
        "232e71b6bcf249c8c1151ba81ea7cce54ff43eec",
        "V2R9",
    ),
)


def _catalog() -> dict[str, CommunityFix]:
    fixes = [
        CommunityFix(
            role,
            profile,
            title,
            "Hardening",
            "DevSec",
            platform,
            "Whole role",
            "Review role variables, supported platforms and collection dependencies before running.",
            _DEVSEC,
            "devsec.hardening." + role,
        )
        for profile, title, platform, role in _DEVSEC_FIXES
    ]
    for profile, platform, repository, version, commit, benchmark in _STIG_FIXES:
        source = Release(
            repository.lower(),
            f"https://github.com/ansible-lockdown/{repository}",
            version,
            commit,
        )
        fixes.append(
            CommunityFix(
                source.name,
                profile,
                f"{platform} STIG {benchmark}",
                "DISA STIG",
                "Ansible-Lockdown",
                platform,
                "Whole role",
                "Compare the benchmark release with your assessment. Upstream does not guarantee check mode.",
                source,
                "lockdown_stig",
            )
        )
    for preset in scap_catalog():
        settings = preset["openscap"]
        if settings["distribution"] != "RedHat" or not settings["profile_id"].endswith(
            "_pci-dss"
        ):
            continue
        major = int(settings["version"].split(".", 1)[0])
        fixes.append(
            CommunityFix(
                f"pci-dss-rhel{major}",
                preset["id"],
                preset["name"],
                "PCI DSS",
                "ComplianceAsCode",
                f"RHEL {major}",
                "Whole profile",
                "Published profile-wide playbook. Review applicable rules and collection dependencies before running.",
                Release(
                    "scap-security-guide",
                    preset["url"],
                    preset["release"],
                    preset["archive_sha256"],
                ),
                rhel_major=major,
            )
        )
    return {item.id: item for item in fixes}


_FIXES = _catalog()
_CINC_URLS = {item["id"]: item["url"] for item in profile_catalog()}
_REVIEW_BYTES = 32_000


def community_fix_catalog() -> list[dict[str, str]]:
    return [
        {**fix.summary(), "profile_url": _CINC_URLS.get(fix.profile_id, "")}
        for fix in _FIXES.values()
    ]


def _fix(fix_id: object) -> CommunityFix:
    if not isinstance(fix_id, str) or fix_id not in _FIXES:
        raise ValueError("Select a fix from the community library")
    return _FIXES[fix_id]


class CommunityFixStore:
    """Cache reviewed source trees and their single-playbook entry points."""

    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root.expanduser().resolve()
        self.root = confined_path(self.projects_root, "fixes")

    def _bundle_path(self, release: Release) -> Path:
        path = self.root / f"{release.name}-{release.commit}"
        if path.resolve() != path:
            raise ValueError("Community fixes storage must not use symlinks")
        return path

    def _entry(
        self, fix: CommunityFix, *, verify: bool = False
    ) -> dict[str, Any] | None:
        if fix.rhel_major:
            path = cached_scap_playbook(
                self.projects_root, fix.rhel_major, verify=verify
            )
            return {"id": fix.id, "playbook": str(path)} if path else None

        bundle = self._bundle_path(fix.release)
        receipt = bundle / "ready.json"
        if not receipt.is_file() or receipt.is_symlink():
            return None
        data = read_document(receipt)
        if (
            not isinstance(data, dict)
            or data.get("commit") != fix.release.commit
            or not re.fullmatch(r"[a-f0-9]{32}", str(data.get("generation", "")))
            or not re.fullmatch(r"[a-f0-9]{64}", str(data.get("sha256", "")))
        ):
            raise ValueError(
                "Invalid community fixes receipt. Import the source again."
            )
        path = bundle / data["generation"]
        if path.resolve() != path:
            raise ValueError("Invalid community fixes bundle path.")
        playbook = path / f"{fix.id}.yml"
        if not playbook.is_file() or playbook.is_symlink():
            return None
        if verify and snapshot_profile(path, None, path, set()) != data["sha256"]:
            raise ValueError(
                "Community fixes bundle changed. Import the pinned source again."
            )
        return {
            "id": fix.id,
            "playbook": str(playbook),
            "bundle_sha256": data["sha256"],
        }

    def list(self) -> list[str]:
        available = []
        for fix in _FIXES.values():
            try:
                if self._entry(fix):
                    available.append(fix.id)
            except (OSError, ValueError):
                continue
        return available

    async def import_fix(self, fix_id: object) -> list[str]:
        fix = _fix(fix_id)
        if fix.rhel_major:
            await prepare_scap_playbook(self.projects_root, fix.rhel_major)
        else:
            async with asyncio.timeout(120):
                await self._import_repository(fix)
        return await asyncio.to_thread(self.list)

    async def _import_repository(self, fix: CommunityFix) -> None:
        bundle = self._bundle_path(fix.release)
        prepare_private_parent(bundle, managed_parent=bundle)
        try:
            with cache_generation_lock(bundle, exclusive=True, blocking=False):
                try:
                    if await asyncio.to_thread(self._entry, fix, verify=True):
                        return
                except ValueError:
                    pass  # Leave the previous generation in place until the new import is complete.
                archive = await GitHubProfiles().download(
                    {
                        "url": fix.release.url,
                        "commit": fix.release.commit,
                    }
                )
                with tempfile.TemporaryDirectory(
                    prefix=".import-", dir=bundle
                ) as directory:
                    stage = Path(directory) / "bundle"
                    build = asyncio.create_task(
                        asyncio.to_thread(self._build, fix, archive, stage)
                    )
                    try:
                        digest = await asyncio.shield(build)
                    except asyncio.CancelledError:
                        await asyncio.gather(build, return_exceptions=True)
                        raise
                    generation = uuid.uuid4().hex
                    stage.rename(bundle / generation)
                    write_private_json(
                        bundle / "ready.json",
                        {
                            "generation": generation,
                            "commit": fix.release.commit,
                            "sha256": digest,
                        },
                        managed_parent=bundle,
                        prefix=".receipt-",
                    )
        except BlockingIOError as exc:
            raise ValueError(
                "This source is being imported. Wait and try again."
            ) from exc

    def _build(self, fix: CommunityFix, archive: bytes, stage: Path) -> str:
        if fix.release == _DEVSEC:
            repository = stage / "collections/ansible_collections/devsec/hardening"
            role_root = repository / "roles"
        else:
            repository = stage / "roles" / fix.role
            role_root = repository.parent
        extract_project(archive, repository)
        for item in _FIXES.values():
            if item.release != fix.release:
                continue
            role = role_root / item.role.rsplit(".", 1)[-1]
            if not any(
                (role / f"tasks/main{ext}").is_file() for ext in (".yml", ".yaml")
            ):
                raise ValueError(
                    "The pinned source does not contain the selected Ansible role"
                )
            playbook = [
                {
                    "name": item.name,
                    "hosts": "all",
                    "become": True,
                    "tasks": [{"ansible.builtin.include_role": {"name": item.role}}],
                }
            ]
            (stage / f"{item.id}.yml").write_text(
                json.dumps(playbook, indent=2) + "\n", encoding="utf-8"
            )
        return snapshot_profile(stage, None, stage, set())

    def review(self, fix_id: object) -> dict[str, Any]:
        fix = _fix(fix_id)
        entry = self._entry(fix, verify=True)
        if not entry:
            raise ValueError("Import the pinned source before reviewing the playbook.")
        path = Path(entry["playbook"])
        with path.open("rb") as handle:
            raw = handle.read(_REVIEW_BYTES + 1)
        role_files = {}
        if fix.role:
            role = (
                path.parent
                / "collections/ansible_collections/devsec/hardening/roles"
                / fix.role.rsplit(".", 1)[-1]
                if fix.release == _DEVSEC
                else path.parent / "roles" / fix.role
            )
            main = next(
                item
                for item in (role / "tasks/main.yml", role / "tasks/main.yaml")
                if item.is_file()
            )
            role_files["role_tasks"] = str(main)
            with main.open("rb") as handle:
                role_files["role_preview"] = handle.read(_REVIEW_BYTES).decode(
                    "utf-8", errors="replace"
                )
        return {
            **fix.summary(),
            **entry,
            **role_files,
            "playbook_sha256": file_digest(path),
            "preview": raw[:_REVIEW_BYTES].decode("utf-8", errors="replace"),
            "preview_truncated": len(raw) > _REVIEW_BYTES,
        }
