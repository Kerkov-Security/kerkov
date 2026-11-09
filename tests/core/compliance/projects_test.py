"""URL validation, bounded downloads and atomic ready-project refresh."""

import asyncio
import base64
import io
import os
import tarfile
import threading
from pathlib import Path

import httpx
import pytest

from kerkov.core.compliance.profile_bundle import profile_bundle_summary
from kerkov.core.compliance.profile_sources import ProfileSource, profile_catalog
from kerkov.core.compliance.projects import (
    GitHubProfiles,
    ProfileProjects,
    extract_project,
)

URL = "https://github.com/example/profile"
COMMIT = "a" * 40


def _create_mock_archive(files=None):
    files = files or {
        "project/inspec.yml": b"name: upstream\nversion: 1.0.0\n",
        "project/controls/check.rb": b"control 'upstream-check' do\n impact 0.5\n title 'Fixture'\n desc 'Fixture'\n describe(1) { it { should eq 1 } }\nend\n",
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def _create_mock_github(
    *,
    status=200,
    metadata=b"name: upstream\nversion: 1.0.0\n",
    redirect=None,
    revision=lambda: COMMIT,
):
    calls = []

    async def respond(request):
        calls.append(str(request.url))
        if status != 200:
            return httpx.Response(status)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": revision()})
        if request.url.path.endswith("/contents/inspec.yml"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(metadata).decode(),
                },
            )
        if "/tarball/" in request.url.path:
            return httpx.Response(
                302,
                headers={
                    "location": redirect
                    or "https://codeload.github.com/example/profile/archive"
                },
            )
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=_create_mock_archive())
        return httpx.Response(200, json={"default_branch": "main"})

    transport = httpx.MockTransport(respond)
    return GitHubProfiles(lambda: httpx.AsyncClient(transport=transport)), calls


class _FakePreparationRuntime:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.started = None
        self.release = None

    async def run(self, prepared, timeout_seconds, *, metadata):
        self.calls.append(prepared.operation)
        (prepared.directory / "scanner.log").write_text("local preparation log")
        if self.started:
            self.started.set()
            await self.release.wait()
        metadata["exit_code"] = 1 if self.fail else 0


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://github.com.evil/a/b",
        "https://user:password@github.com/a/b",
        "https://github.com:443/a/b",
        "https://127.0.0.1/a/b",
        "file:///tmp/project",
        "https://github.com/a/b/issues/1",
        "https://github.com/a/b/tree/a/b",
        "https://github.com/a/%2e%2e",
        "https://github.com/a/b?token=secret",
        "https://github.com/a/b#readme",
    ],
)
def test_invalid_sources_fail_before_any_http_request(url):
    remote, calls = _create_mock_github()
    with pytest.raises(ValueError):
        asyncio.run(remote.validate(url))
    assert calls == []


def test_catalog_contains_only_source_definitions_and_canonical_urls():
    for item in profile_catalog():
        assert ProfileSource.parse(item["url"]).url == item["url"]
        assert not {"profile", "controls", "playbook"} & item.keys()
    assert ProfileSource.parse("https://github.com/Example/Profile.git/").url == URL
    assert ProfileSource.parse(URL + "/tree/v1.0").ref == "v1.0"


def test_valid_url_checks_repository_revision_and_metadata_before_download():
    remote, calls = _create_mock_github()
    result = asyncio.run(remote.validate(URL))
    assert result == {
        "url": URL,
        "commit": COMMIT,
        "ref": "main",
        "name": "upstream",
        "version": "1.0.0",
        "has_dependencies": False,
    }
    assert len(calls) == 3
    assert not any("tarball" in call for call in calls)
    archive = asyncio.run(remote.download(result))
    assert archive.startswith(b"\x1f\x8b")


@pytest.mark.parametrize("status", [404, 403, 429, 500])
def test_unavailable_and_rate_limited_sources_are_actionable(status):
    remote, _ = _create_mock_github(status=status)
    with pytest.raises(ValueError):
        asyncio.run(remote.validate(URL))


@pytest.mark.parametrize(
    "metadata",
    [
        b"name: [bad]",
        b"not-profile",
        b"name: valid\ngem_dependencies: [{name: external}]",
        b"name: [invalid yaml",
    ],
)
def test_non_profiles_and_unsupported_metadata_do_not_validate(metadata):
    remote, calls = _create_mock_github(metadata=metadata)
    with pytest.raises(ValueError):
        asyncio.run(remote.validate(URL))
    assert not any("tarball" in call for call in calls)


def test_archive_redirects_cannot_leave_github():
    remote, calls = _create_mock_github(redirect="http://127.0.0.1/private")
    validated = asyncio.run(remote.validate(URL))
    with pytest.raises(ValueError, match="redirect"):
        asyncio.run(remote.download(validated))
    assert not any("127.0.0.1" in call for call in calls)


@pytest.mark.parametrize(
    "path",
    ["../escape", "root/../../escape", "/absolute", "root/\\windows", "other/file"],
)
def test_archive_extraction_rejects_escaping_paths_and_multiple_roots(tmp_path, path):
    archive = _create_mock_archive(
        {"root/inspec.yml": b"name: example", path: b"invalid"}
    )
    with pytest.raises(ValueError):
        extract_project(archive, tmp_path / "project")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_archive_links_and_special_files_are_rejected(tmp_path, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        entry = tarfile.TarInfo("root/link")
        entry.type = kind
        entry.linkname = "/etc/passwd"
        archive.addfile(entry)
    with pytest.raises(ValueError):
        extract_project(output.getvalue(), tmp_path)


def test_archive_file_and_expansion_limits_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr("kerkov.core.compliance.projects.MAX_PROFILE_BYTES", 5)
    with pytest.raises(ValueError):
        extract_project(_create_mock_archive(), tmp_path)


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_ready_copy_is_reused_offline_and_failed_refresh_keeps_it(tmp_path):
    async def scenario():
        remote, calls = _create_mock_github()
        runtime = _FakePreparationRuntime()
        projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=runtime)
        first = await projects.prepare(URL, tmp_path / "workspace")
        assert not first["reused"]
        assert runtime.calls == ["check"]
        assert (
            profile_bundle_summary(Path(first["profile"]))["source"]["commit"] == COMMIT
        )
        count = len(calls)
        second = await projects.prepare(URL, tmp_path / "workspace")
        assert second["reused"] and second["profile"] == first["profile"]
        assert len(calls) == count
        with pytest.raises(ValueError, match="different commit"):
            await projects.prepare(URL, tmp_path / "workspace", commit="b" * 40)
        assert len(calls) == count
        assert projects.get(URL)["generation"] == first["generation"]
        runtime.fail = True
        with pytest.raises(ValueError, match="previous ready copy"):
            await projects.prepare(URL, tmp_path / "workspace", refresh=True)
        assert projects.get(URL, verify=True)["profile"] == first["profile"]
        runtime.fail = False
        refreshed = await projects.prepare(URL, tmp_path / "workspace", refresh=True)
        assert refreshed["profile"] != first["profile"]
        assert refreshed["profile_sha256"] == first["profile_sha256"]
        assert Path(first["profile"]).is_dir()
        assert projects.list()[0]["generation"] == refreshed["generation"]

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_cancellation_releases_project_lock_and_preserves_ready_copy(tmp_path):
    async def scenario():
        remote, _ = _create_mock_github()
        runtime = _FakePreparationRuntime()
        projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=runtime)
        first = await projects.prepare(URL, tmp_path / "workspace")
        runtime.started, runtime.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(
            projects.prepare(URL, tmp_path / "workspace", refresh=True)
        )
        await runtime.started.wait()
        with pytest.raises(ValueError, match="already being prepared"):
            await projects.prepare(URL, tmp_path / "workspace", refresh=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert projects.get(URL, verify=True)["profile"] == first["profile"]
        runtime.started = None
        assert (await projects.prepare(URL, tmp_path / "workspace"))["reused"]

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_refresh_can_repair_a_changed_cached_copy(tmp_path):
    async def scenario():
        remote, _ = _create_mock_github()
        projects = ProfileProjects(
            tmp_path / "cache", remote=remote, runtime=_FakePreparationRuntime()
        )
        first = await projects.prepare(URL, tmp_path / "workspace")
        (Path(first["profile"]) / "controls/check.rb").write_text("changed")
        with pytest.raises(ValueError, match="changed"):
            await projects.prepare(URL, tmp_path / "workspace")
        assert not (await projects.prepare(URL, tmp_path / "workspace", refresh=True))[
            "reused"
        ]

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_cancellation_during_cache_copy_does_not_publish_a_new_version(
    tmp_path, monkeypatch
):
    async def scenario():
        remote, _ = _create_mock_github()
        projects = ProfileProjects(
            tmp_path / "cache", remote=remote, runtime=_FakePreparationRuntime()
        )
        first = await projects.prepare(URL, tmp_path / "workspace")
        entered, release = threading.Event(), threading.Event()
        original = projects._stage_version

        def copy(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(projects, "_stage_version", copy)
        task = asyncio.create_task(
            projects.prepare(URL, tmp_path / "workspace", refresh=True)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert projects.get(URL)["generation"] == first["generation"]

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_existing_custom_cache_root_permissions_are_preserved(tmp_path):
    async def scenario():
        cache = tmp_path / "existing"
        cache.mkdir(mode=0o755)
        before = cache.stat().st_mode
        remote, _ = _create_mock_github()
        projects = ProfileProjects(
            cache, remote=remote, runtime=_FakePreparationRuntime()
        )
        await projects.prepare(URL, tmp_path / "workspace")
        assert cache.stat().st_mode == before

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_preparation_cannot_follow_a_created_provenance_symlink(tmp_path):
    async def scenario():
        remote, _ = _create_mock_github()
        runtime = _FakePreparationRuntime()
        original = runtime.run
        outside = tmp_path / "must-not-be-created"

        async def run(prepared, timeout_seconds, *, metadata):
            await original(prepared, timeout_seconds, metadata=metadata)
            (prepared.directory / "profile/.kerkov-source.json").symlink_to(outside)

        runtime.run = run
        projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=runtime)
        with pytest.raises(ValueError, match="symlinks"):
            await projects.prepare(URL, tmp_path / "workspace")
        assert not outside.exists()
        assert projects.get(URL) is None

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.name == "nt", reason="Profile preparation controller requires POSIX"
)
def test_cache_version_parent_cannot_redirect_publication_outside_cache(tmp_path):
    async def scenario():
        source = ProfileSource.parse(URL)
        root = tmp_path / "cache"
        item = root / source.key
        item.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (item / "versions").symlink_to(outside, target_is_directory=True)
        remote, _ = _create_mock_github()
        projects = ProfileProjects(
            root, remote=remote, runtime=_FakePreparationRuntime()
        )
        with pytest.raises(ValueError, match="escapes"):
            await projects.prepare(URL, tmp_path / "workspace")
        assert list(outside.iterdir()) == []

    asyncio.run(scenario())
