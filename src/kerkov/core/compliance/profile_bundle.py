"""Validate existing InSpec projects and their prepared offline dependencies."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kerkov.core.automation.profiles import read_document

MAX_DEPENDENCIES = 64
MAX_DEPENDENCY_DEPTH = 8
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_COMMIT = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
_SOURCE_KEYS = {
    "sha256",
    "path",
    "git",
    "url",
    "ref",
    "branch",
    "tag",
    "commit",
    "relative_path",
}


def _document(root: Path, path: Path) -> Any:
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Profile bundle files must exist inside the project directory")
    return read_document(path)


def validate_profile_metadata(metadata: Any) -> dict[str, Any]:
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("name"), str)
        or not _NAME.fullmatch(metadata["name"])
    ):
        raise ValueError("profile inspec.yml requires an ordinary profile name")
    if any(metadata.get(key) for key in ("gem_dependencies", "gems")):
        raise ValueError(
            "Profile gem dependencies are unsupported; scans do not install gems"
        )
    version = metadata.get("version")
    if version is not None and (
        not isinstance(version, (str, int, float)) or isinstance(version, bool)
    ):
        raise ValueError("Profile version must be a scalar string or number")
    _names([] if metadata.get("depends") is None else metadata["depends"])
    return metadata


def _metadata(root: Path, directory: Path) -> dict[str, Any]:
    return validate_profile_metadata(_document(root, directory / "inspec.yml"))


def profile_bundle_summary(source: Path) -> dict[str, Any]:
    metadata, dependencies = inspect_profile_bundle(source)
    summary = {
        "name": metadata["name"],
        "version": str(metadata.get("version", ""))[:100],
        "dependencies": dependencies,
    }
    provenance = source / ".kerkov-source.json"
    if provenance.exists():
        from kerkov.core.compliance.profile_sources import ProfileSource

        origin = _document(source, provenance)
        if (
            not isinstance(origin, dict)
            or not isinstance(origin.get("commit"), str)
            or not _COMMIT.fullmatch(origin["commit"])
        ):
            raise ValueError("Invalid downloaded project provenance")
        url = ProfileSource.parse(origin.get("url")).url
        summary["source"] = {
            "url": url,
            "commit": origin["commit"],
        }
    return summary


def _names(dependencies: Any) -> list[str]:
    if not isinstance(dependencies, list) or len(dependencies) > MAX_DEPENDENCIES:
        raise ValueError("Profile dependencies must be a list of at most 64 entries")
    names = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or not isinstance(dependency.get("name"), str)
            or not _NAME.fullmatch(dependency["name"])
        ):
            raise ValueError("Each profile dependency requires an ordinary name")
        names.append(dependency["name"])
    if len(names) != len(set(names)):
        raise ValueError("Profile dependency names must be unique within each parent")
    return names


def inspect_profile_bundle(source: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Require every locked dependency to be an unpacked local vendor-cache entry."""
    metadata = _metadata(source, source)
    details: list[dict[str, Any]] = []

    def visit(
        directory: Path,
        current: dict[str, Any],
        entries: Any,
        ancestors: tuple[str, ...],
    ) -> None:
        declared_dependencies = current.get("depends")
        declared = _names(
            [] if declared_dependencies is None else declared_dependencies
        )
        if entries is None:
            lock_path = directory / "inspec.lock"
            if not lock_path.exists():
                if declared:
                    raise ValueError(
                        "Prepare profile dependencies with inspec vendor before scanning; inspec.lock and an unpacked vendor directory are required"
                    )
                return
            lock = _document(source, lock_path)
            if (
                not isinstance(lock, dict)
                or type(lock.get("lockfile_version")) is not int
                or lock["lockfile_version"] != 1
            ):
                raise ValueError("Profile bundle requires an InSpec version-1 lockfile")
            entries = lock.get("depends", [])
        if set(_names(entries)) != set(declared):
            raise ValueError(
                "Locked dependencies must match each profile's declared dependencies"
            )
        for entry in entries:
            if (
                len(details) >= MAX_DEPENDENCIES
                or len(ancestors) >= MAX_DEPENDENCY_DEPTH
            ):
                raise ValueError(
                    "Profile bundle exceeds 64 dependencies or 8 dependency levels"
                )
            resolved = entry.get("resolved_source")
            if (
                not isinstance(resolved, dict)
                or set(resolved) - _SOURCE_KEYS
                or not any(
                    isinstance(resolved.get(key), str) and resolved[key]
                    for key in ("path", "git", "url")
                )
            ):
                raise ValueError(
                    "Each dependency requires a locked path, git or URL source"
                )
            digest = resolved.get("sha256")
            if digest is not None:
                valid_key = isinstance(digest, str) and _DIGEST.fullmatch(digest)
            else:
                digest = resolved.get("ref")
                valid_key = (
                    isinstance(resolved.get("git"), str)
                    and isinstance(digest, str)
                    and _COMMIT.fullmatch(digest)
                )
            if not isinstance(digest, str) or not valid_key:
                raise ValueError(
                    "Each dependency requires a locked SHA-256 or a full Git commit"
                )
            if digest in ancestors:
                raise ValueError("Profile dependency graph contains a cycle")
            dependency = source / "vendor" / digest
            if (
                not dependency.resolve().is_relative_to(source.resolve())
                or not dependency.is_dir()
            ):
                raise ValueError(
                    "Missing unpacked vendor dependency; prepare the complete bundle before scanning"
                )
            if (dependency / ".lock").exists():
                raise ValueError(
                    "Vendor dependency is locked; finish preparation before scanning"
                )
            if (dependency / "vendor").exists():
                raise ValueError(
                    "Flatten dependency vendor directories into the project vendor directory before scanning"
                )
            child = _metadata(source, dependency)
            details.append(
                {
                    "name": entry["name"],
                    "profile_name": child["name"],
                    "version": str(child.get("version", ""))[:100],
                    "cache_key": digest,
                }
            )
            children = entry.get("dependencies", [])
            _names(children)
            visit(
                dependency, child, children if children else None, (*ancestors, digest)
            )

    visit(source, metadata, None, ())
    return metadata, details
