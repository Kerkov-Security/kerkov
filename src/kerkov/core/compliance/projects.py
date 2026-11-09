"""Validate, prepare and explicitly refresh upstream compliance projects."""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import logging
import os
import re
import shutil
import tarfile
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, TypeVar
from urllib.parse import quote, urljoin, urlsplit

import httpx
import yaml  # type: ignore[import-untyped]

from kerkov.core.automation.profiles import read_document
from kerkov.core.compliance.cinc_runtime import (
    MAX_PROFILE_BYTES,
    MAX_PROFILE_FILES,
    DockerCincRuntime,
    PreparedCincProject,
    snapshot_profile,
)
from kerkov.core.compliance.profile_bundle import (
    MAX_DEPENDENCIES,
    MAX_DEPENDENCY_DEPTH,
    inspect_profile_bundle,
    profile_bundle_summary,
    validate_profile_metadata,
)
from kerkov.core.compliance.profile_sources import COMMIT_PATTERN, ProfileSource
from kerkov.core.compliance.reports import new_directory
from kerkov.core.private_files import prepare_private_parent

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def _disk(function: Callable[..., T], *args: Any) -> T:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class GitHubProfiles:
    def __init__(self, client_factory: Callable[[], httpx.AsyncClient] | None = None):
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=30,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "Kerkov-compliance"},
            )
        )

    async def _read(
        self, client: httpx.AsyncClient, url: str, limit: int
    ) -> tuple[httpx.Response, bytes]:
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                return response, b""
            if response.status_code != 200:
                if response.status_code == 404:
                    raise ValueError(
                        "Public repository, ref or root inspec.yml was not found"
                    )
                if response.status_code in {403, 429}:
                    raise ValueError(
                        "GitHub refused the request or its public API rate limit was reached; retry later"
                    )
                raise ValueError(f"GitHub returned HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > limit:
                    raise ValueError(
                        "Upstream response exceeds the project download limit"
                    )
                body.extend(chunk)
            return response, bytes(body)

    async def _json(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        response, raw = await self._read(client, url, 4 * 1024 * 1024)
        if response.is_redirect:
            raise ValueError("Use the repository's current canonical GitHub URL")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid GitHub repository response")
        return value

    async def _metadata(
        self, client: httpx.AsyncClient, source: ProfileSource, commit: str
    ) -> dict[str, Any]:
        file = await self._json(
            client, source.api + "/contents/inspec.yml?ref=" + commit
        )
        if (
            file.get("type") != "file"
            or file.get("encoding") != "base64"
            or not isinstance(file.get("content"), str)
        ):
            raise ValueError("Repository root must contain an ordinary inspec.yml file")
        raw = base64.b64decode("".join(file["content"].split()), validate=True)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Profile metadata exceeds the 2 MiB limit")
        return await _disk(lambda: validate_profile_metadata(yaml.safe_load(raw)))

    async def _dependencies(
        self, client: httpx.AsyncClient, metadata: dict[str, Any], commit: str
    ) -> list[dict[str, Any]]:
        count = 0
        fetched: dict[tuple[ProfileSource, str], list[dict[str, str]]] = {}

        async def visit(
            entries: list[dict[str, str]], ancestors: tuple[str, ...]
        ) -> list[dict[str, Any]]:
            nonlocal count
            result = []
            for entry in entries:
                count += 1
                if count > MAX_DEPENDENCIES or len(ancestors) > MAX_DEPENDENCY_DEPTH:
                    raise ValueError(
                        "Profile bundle exceeds 64 dependencies or 8 dependency levels"
                    )
                revision = entry["ref"]
                if revision in ancestors:
                    raise ValueError("Profile dependency graph contains a cycle")
                source = ProfileSource.parse(entry["git"])
                key = (source, revision)
                if key not in fetched:
                    fetched[key] = _dependency_sources(
                        await self._metadata(client, source, revision)
                    )
                children = await visit(fetched[key], (*ancestors, revision))
                result.append({**entry, "dependencies": children})
            return result

        return await visit(_dependency_sources(metadata), (commit,))

    async def validate(self, url: str) -> dict[str, Any]:
        source = ProfileSource.parse(url)
        try:
            async with self.client_factory() as client:
                repository = await self._json(client, source.api)
                ref = source.ref or repository.get("default_branch")
                if not isinstance(ref, str) or not 1 <= len(ref) <= 255:
                    raise ValueError("Repository has no usable default branch")
                revision = await self._json(
                    client, source.api + "/commits/" + quote(ref, safe="")
                )
                commit = revision.get("sha")
                if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
                    raise ValueError("Repository has no usable revision")
                metadata = await self._metadata(client, source, commit)
                dependencies = await self._dependencies(client, metadata, commit)
                return {
                    "url": source.url,
                    "commit": commit,
                    "ref": ref,
                    "name": metadata["name"],
                    "version": str(metadata.get("version", ""))[:100],
                    "has_dependencies": bool(dependencies),
                    "dependencies": dependencies,
                }
        except httpx.HTTPError as exc:
            raise ValueError(
                "Could not reach GitHub; check connectivity and retry"
            ) from exc
        except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
            raise ValueError("Repository inspec.yml is not valid profile YAML") from exc

    async def download(self, validated: dict[str, Any]) -> bytes:
        source = ProfileSource.parse(validated["url"])
        url = source.api + "/tarball/" + validated["commit"]
        try:
            async with self.client_factory() as client:
                for _ in range(4):
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or parsed.netloc.lower() not in {
                        "api.github.com",
                        "codeload.github.com",
                    }:
                        raise ValueError("Unexpected GitHub archive redirect")
                    response, raw = await self._read(client, url, MAX_PROFILE_BYTES)
                    if not response.is_redirect:
                        return raw
                    url = urljoin(url, response.headers.get("location", ""))
            raise ValueError("Too many GitHub archive redirects")
        except httpx.HTTPError as exc:
            raise ValueError(
                "Project download failed; check connectivity and retry"
            ) from exc


def _dependency_sources(metadata: dict[str, Any]) -> list[dict[str, str]]:
    sources = []
    for entry in metadata.get("depends") or []:
        name = entry["name"]
        commit = entry.get("ref")
        if (
            set(entry) != {"name", "git", "ref"}
            or not isinstance(commit, str)
            or not COMMIT_PATTERN.fullmatch(commit)
        ):
            raise ValueError(
                f"Dependency {name} must use a public GitHub git URL and a full commit in ref"
            )
        try:
            source = ProfileSource.parse(entry["git"])
        except ValueError as exc:
            raise ValueError(
                f"Dependency {name} requires a public GitHub git URL"
            ) from exc
        if source.ref is not None:
            raise ValueError(f"Dependency {name} must use a root GitHub repository URL")
        sources.append({"name": name, "git": source.url, "ref": commit})
    return sources


def _discard_upstream_lock(stage: Path, dependencies: Sequence[dict[str, Any]]) -> None:
    metadata = validate_profile_metadata(read_document(stage / "inspec.yml"))
    declared = [
        {key: entry[key] for key in ("name", "git", "ref")} for entry in dependencies
    ]
    if _dependency_sources(metadata) != declared:
        raise ValueError("Downloaded project dependencies do not match URL validation")
    vendor = stage / "vendor"
    if vendor.is_dir():
        shutil.rmtree(vendor)
    elif vendor.exists():
        vendor.unlink()
    (stage / "inspec.lock").unlink(missing_ok=True)


def extract_project(archive: bytes, destination: Path) -> None:
    limit = MAX_PROFILE_BYTES + MAX_PROFILE_FILES * 2048
    with gzip.GzipFile(fileobj=io.BytesIO(archive)) as compressed:
        raw = compressed.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Expanded project archive exceeds the processing limit")
    total = 0
    prefix = None
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive_file:
        for count, member in enumerate(archive_file, 1):
            parts = PurePosixPath(member.name).parts
            if (
                count > MAX_PROFILE_FILES
                or not parts
                or member.name.startswith("/")
                or ".." in parts
                or "\\" in member.name
            ):
                raise ValueError(
                    "Project archive contains an invalid or oversized path list"
                )
            if prefix is None:
                prefix = parts[0]
            if parts[0] != prefix or not (member.isdir() or member.isfile()):
                raise ValueError(
                    "Project archive must have one root and ordinary files; links are unsupported"
                )
            path = destination.joinpath(*parts[1:])
            if not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Project archive path escapes its directory")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            if (
                len(parts) < 2
                or member.size < 0
                or total + member.size > MAX_PROFILE_BYTES
                or path.exists()
            ):
                raise ValueError(
                    "Project archive has duplicate files or exceeds the 64 MiB limit"
                )
            total += member.size
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = archive_file.extractfile(member)
            if handle is None:
                raise ValueError("Project archive file is unavailable")
            with handle:
                content = handle.read(member.size + 1)
            if len(content) != member.size:
                raise ValueError("Project archive is incomplete")
            path.write_bytes(content)


class ProfileProjects:
    def __init__(
        self,
        root: Path,
        *,
        remote: GitHubProfiles | None = None,
        runtime: DockerCincRuntime | None = None,
        image: str = "cincproject/auditor:7",
    ):
        self.root = root.expanduser().resolve()
        self.remote = remote or GitHubProfiles()
        self.runtime = runtime or DockerCincRuntime(image=image)

    def get(self, url: str, *, verify: bool = False) -> dict[str, Any] | None:
        source = ProfileSource.parse(url)
        pointer = self.root / source.key / "ready.json"
        if not pointer.exists():
            return None
        if pointer.is_symlink() or not pointer.resolve().is_relative_to(self.root):
            raise ValueError("Cached project record must stay inside the cache")
        value = read_document(pointer)
        if (
            not isinstance(value, dict)
            or value.get("url") != source.url
            or not re.fullmatch(r"[a-f0-9]{32}", str(value.get("generation", "")))
        ):
            raise ValueError("Cached project record is invalid")
        profile = self.root / source.key / "versions" / value["generation"] / "profile"
        if not profile.resolve().is_relative_to(self.root) or not profile.is_dir():
            raise ValueError("Cached project files are unavailable")
        if verify and snapshot_profile(profile, None, self.root, set()) != value.get(
            "profile_sha256"
        ):
            raise ValueError("Cached project changed; Refresh it before use")
        return {**value, "profile": str(profile)}

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        result = []
        for path in sorted(self.root.iterdir())[:100]:
            if not re.fullmatch(r"[a-f0-9]{64}", path.name) or path.is_symlink():
                continue
            try:
                pointer = path / "ready.json"
                if pointer.is_symlink() or not pointer.resolve().is_relative_to(
                    self.root
                ):
                    continue
                value = read_document(pointer)
                record = (
                    self.get(value.get("url", "")) if isinstance(value, dict) else None
                )
                if record:
                    result.append(record)
            except (OSError, ValueError, TypeError):
                continue
        return result

    def _lock(self, source: ProfileSource) -> int:
        import fcntl

        item = self.root / source.key
        prepare_private_parent(
            self.root, managed_parent=Path.home() / ".kerkov/compliance/projects"
        )
        if not item.resolve().is_relative_to(self.root):
            raise ValueError("Project cache directory escapes the configured root")
        prepare_private_parent(item, managed_parent=item)
        if item.stat().st_uid != os.geteuid():
            raise PermissionError("Project cache directory must be owned by you")
        descriptor = os.open(
            item / "preparing.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise ValueError(
                "This project is already being prepared; wait and retry"
            ) from exc
        return descriptor

    def _stage_version(
        self,
        source: ProfileSource,
        stage: Path,
        validated: dict[str, Any],
        workspace: Path,
        generation: str,
    ) -> dict[str, Any]:
        snapshot_profile(stage, None, workspace, set())
        item = self.root / source.key
        version = item / "versions" / generation
        if not version.resolve().is_relative_to(self.root):
            raise ValueError("Project version directory escapes the cache")
        prepare_private_parent(version, managed_parent=version)
        try:
            prepared_at = datetime.now(timezone.utc).isoformat()
            descriptor = os.open(
                stage / ".kerkov-source.json",
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w") as handle:
                json.dump({"url": source.url, "commit": validated["commit"]}, handle)
            digest = snapshot_profile(stage, version / "profile", workspace, set())
            summary = profile_bundle_summary(version / "profile")
            record = {
                "generation": generation,
                "url": source.url,
                "commit": validated["commit"],
                "ref": validated["ref"],
                "name": summary["name"],
                "version": summary["version"],
                "dependencies": summary["dependencies"],
                "prepared_at": prepared_at,
                "profile_sha256": digest,
            }
            temporary = item / (generation + ".json")
            with temporary.open("x") as handle:
                json.dump(record, handle)
                handle.flush()
                os.fsync(handle.fileno())
            return {**record, "profile": str(version / "profile")}
        except BaseException:
            if version.resolve().is_relative_to(self.root):
                shutil.rmtree(version)
            raise

    def _prune_versions(self, source: ProfileSource, keep: str) -> None:
        item = self.root / source.key
        versions = item / "versions"
        for version in versions.iterdir():
            if version.name != keep and re.fullmatch(r"[a-f0-9]{32}", version.name):
                self._discard_version(source, version.name)

    def _discard_version(self, source: ProfileSource, generation: str) -> None:
        item = self.root / source.key
        version = item / "versions" / generation
        try:
            if (
                version.is_dir()
                and not version.is_symlink()
                and version.resolve().is_relative_to(item)
            ):
                shutil.rmtree(version)
            (item / (generation + ".json")).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not remove cached profile generation %s",
                generation,
                exc_info=True,
            )

    async def _bundle_dependencies(
        self, stage: Path, workspace: Path, dependencies: Sequence[dict[str, Any]]
    ) -> None:
        await _disk(_discard_upstream_lock, stage, dependencies)
        fetched: set[str] = set()

        async def visit(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
            locked = []
            for entry in entries:
                commit = entry["ref"]
                if commit not in fetched:
                    destination = stage / "vendor" / commit
                    archive = await self.remote.download(
                        {"url": entry["git"], "commit": commit}
                    )
                    await _disk(extract_project, archive, destination)
                    await _disk(
                        _discard_upstream_lock, destination, entry["dependencies"]
                    )
                    await _disk(snapshot_profile, stage, None, workspace, set())
                    fetched.add(commit)
                locked.append(
                    {
                        "name": entry["name"],
                        "resolved_source": {"git": entry["git"], "ref": commit},
                        "version_constraints": [],
                        "dependencies": await visit(entry["dependencies"]),
                    }
                )
            return locked

        if dependencies:
            lock = {"lockfile_version": 1, "depends": await visit(dependencies)}
            await _disk(
                lambda: (stage / "inspec.lock").write_text(
                    yaml.safe_dump(lock), encoding="utf-8"
                )
            )

    async def prepare(
        self,
        url: str,
        workspace: Path,
        *,
        refresh: bool = False,
        expected_commit: str | None = None,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        source = ProfileSource.parse(url)
        acquisition = asyncio.create_task(asyncio.to_thread(self._lock, source))
        try:
            descriptor = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            descriptor = await acquisition
            os.close(descriptor)
            raise
        generation = None
        published = False
        try:
            async with asyncio.timeout(timeout_seconds):
                existing = (
                    await _disk(lambda: self.get(source.url, verify=True))
                    if not refresh
                    else None
                )
                if existing is not None:
                    if (
                        expected_commit is not None
                        and existing["commit"] != expected_commit
                    ):
                        raise ValueError(
                            "The ready copy uses a different revision; reselect it or refresh"
                        )
                    return {**existing, "reused": True}
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                if expected_commit is None:
                    raise ValueError(
                        "Validate the project and review its dependencies before preparing; pass the validated commit as expected_commit"
                    )
                validated = await self.remote.validate(source.url)
                if validated["commit"] != expected_commit:
                    raise ValueError(
                        "The upstream revision changed; validate it again before preparing"
                    )
                archive = await self.remote.download(validated)
                directory = await _disk(new_directory, workspace, "project-")
                stage = directory / "profile"
                await _disk(extract_project, archive, stage)
                await self._bundle_dependencies(
                    stage, workspace, validated["dependencies"]
                )
                metadata, _ = await _disk(inspect_profile_bundle, stage)
                if metadata["name"] != validated["name"]:
                    raise ValueError(
                        "Downloaded project metadata does not match URL validation"
                    )
                info: dict[str, Any] = {}
                await self.runtime.run(
                    PreparedCincProject(directory, uuid.uuid4().hex),
                    max(1, deadline - asyncio.get_running_loop().time()),
                    metadata=info,
                )
                if info.get("exit_code") != 0 or info.get("cleanup_error"):
                    raise ValueError(
                        f"Project check failed; inspect {directory / 'scanner.log'}. The previous ready copy was kept"
                    )
                generation = uuid.uuid4().hex
                record = await _disk(
                    self._stage_version, source, stage, validated, workspace, generation
                )
                item = self.root / source.key
                # Publish only after cancellable preparation has finished.
                (item / (generation + ".json")).replace(item / "ready.json")
                published = True
                return {
                    **record,
                    "reused": False,
                    "log_artifact": str(directory / "scanner.log"),
                }
        finally:
            try:
                if generation is not None:
                    cleanup = (
                        self._prune_versions if published else self._discard_version
                    )
                    try:
                        await _disk(cleanup, source, generation)
                    except OSError:
                        logger.warning(
                            "Could not enumerate cached profile generations",
                            exc_info=True,
                        )
            finally:
                os.close(descriptor)
