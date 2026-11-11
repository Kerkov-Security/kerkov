"""Pinned prepared datastreams share project caching without executing a build."""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from kerkov.core.compliance.openscap import datastream_bytes, inspect_datastream
from kerkov.core.compliance.profile_sources import ProfileSource
from kerkov.core.compliance.projects import GitHubProfiles, ProfileProjects
from tests.core.compliance.projects_test import COMMIT, URL, _create_mock_archive

DATASTREAM = "content/server-ds.xml"
PROFILE = "xccdf_example_profile_server"


def _create_mock_datastream(title="Server baseline"):
    return (
        '<data-stream-collection xmlns="http://scap.nist.gov/schema/scap/source/1.2" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" id="collection">'
        '<data-stream id="stream"><checklists><component-ref id="ref" '
        'xlink:href="#component"/></checklists></data-stream>'
        '<component id="component"><Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" '
        'id="xccdf_example_benchmark_server">'
        f"<title>{title}</title><version>1.0</version>"
        f'<Profile id="{PROFILE}"><title>Server STIG</title>'
        "<description>Server settings</description></Profile>"
        '<Profile id="xccdf_example_profile_minimal"><title>Minimal</title></Profile>'
        "</Benchmark></component></data-stream-collection>"
    ).encode()


def _create_mock_scap_github():
    state = {"commit": COMMIT, "raw": _create_mock_datastream(), "archive_raw": None}
    calls = []

    async def respond(request):
        calls.append(str(request.url))
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": state["commit"]})
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={"status": "behind"})
        if "/contents/" in request.url.path:
            assert request.headers["accept"] == "application/vnd.github.raw+json"
            assert request.url.params["ref"] in {COMMIT, "b" * 40}
            return httpx.Response(200, content=state["raw"])
        if "/tarball/" in request.url.path:
            raw = state["archive_raw"] or state["raw"]
            return httpx.Response(
                200,
                content=_create_mock_archive(
                    {
                        "project/" + DATASTREAM: raw,
                        "project/content/other-ds.xml": raw,
                        "project/build.sh": b"exit 99\n",
                    }
                ),
            )
        return httpx.Response(200, json={"default_branch": "main"})

    remote = GitHubProfiles(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    return remote, state, calls


def test_prepare_discovers_profiles_pins_bytes_and_reuses_without_network(tmp_path):
    remote, state, calls = _create_mock_scap_github()
    scanner = AsyncMock()
    projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=scanner)

    async def scenario():
        validated = await remote.validate(URL, datastream=DATASTREAM)
        assert validated["profiles"][0]["title"] == "Server STIG"
        assert validated["profile_sha256"] == hashlib.sha256(state["raw"]).hexdigest()
        assert not any("tarball" in call for call in calls)
        ready = await projects.prepare(
            URL, tmp_path / "run", datastream=DATASTREAM, expected_commit=COMMIT
        )
        assert Path(ready["profile"]).read_bytes() == state["raw"]
        assert ready["engine"] == "openscap"
        assert projects.get(URL) is None
        assert (
            projects.get(URL, datastream=DATASTREAM, verify=True)["generation"]
            == ready["generation"]
        )
        calls.clear()
        reused = await projects.prepare(URL, tmp_path / "reuse", datastream=DATASTREAM)
        assert reused["reused"] and calls == []
        origin = {}
        assert (
            datastream_bytes(
                Path(ready["profile"]),
                ready["profile_sha256"],
                PROFILE,
                projects_root=projects.root,
                source_metadata=origin,
            )
            == state["raw"]
        )
        assert origin["commit"] == COMMIT and origin["datastream"] == DATASTREAM
        second = await projects.prepare(
            URL,
            tmp_path / "other",
            datastream="content/other-ds.xml",
            expected_commit=COMMIT,
        )
        assert second["profile"] != ready["profile"]
        assert len(projects.list()) == 2
        scanner.run.assert_not_called()

    asyncio.run(scenario())


def test_failed_refresh_preserves_previous_generation_and_success_prunes_it(tmp_path):
    remote, state, _ = _create_mock_scap_github()
    projects = ProfileProjects(tmp_path / "cache", remote=remote, runtime=AsyncMock())

    async def scenario():
        ready = await projects.prepare(
            URL, tmp_path / "run", datastream=DATASTREAM, expected_commit=COMMIT
        )
        state.update(commit="b" * 40, archive_raw=_create_mock_datastream("Changed"))
        with pytest.raises(ValueError, match="differs from the reviewed"):
            await projects.prepare(
                URL,
                tmp_path / "failed",
                datastream=DATASTREAM,
                expected_commit="b" * 40,
                refresh=True,
            )
        assert (
            projects.get(URL, datastream=DATASTREAM, verify=True)["generation"]
            == ready["generation"]
        )
        state.update(raw=state["archive_raw"], archive_raw=None)
        updated = await projects.prepare(
            URL,
            tmp_path / "updated",
            datastream=DATASTREAM,
            expected_commit="b" * 40,
            refresh=True,
        )
        assert updated["commit"] == "b" * 40
        assert not Path(ready["profile"]).exists()
        Path(updated["profile"]).write_bytes(_create_mock_datastream("Tampered"))
        with pytest.raises(ValueError, match="changed"):
            projects.get(URL, datastream=DATASTREAM, verify=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    [
        "../file.xml",
        "/file.xml",
        "a/../file.xml",
        "a//file.xml",
        "a\\file.xml",
        "source.yml",
        "",
    ],
)
def test_datastream_paths_fail_before_network(path):
    remote, _, calls = _create_mock_scap_github()
    with pytest.raises(ValueError, match="repository-relative"):
        asyncio.run(remote.validate(URL, datastream=path))
    assert calls == []


@pytest.mark.parametrize(
    "raw",
    [
        b"<Benchmark/>",
        b"<!DOCTYPE doc [<!ENTITY value 'x'>]><doc/>",
        _create_mock_datastream().replace(
            b'xlink:href="#component"', b'xlink:href="https://example.com/content.xml"'
        ),
        _create_mock_datastream().replace(
            b"xccdf_example_profile_minimal", PROFILE.encode()
        ),
    ],
)
def test_unprepared_external_or_ambiguous_content_is_rejected(raw):
    with pytest.raises(ValueError):
        inspect_datastream(raw)


def test_cache_keys_separate_inspec_and_each_datastream():
    sources = [
        ProfileSource.parse(URL),
        ProfileSource.parse(URL, datastream=DATASTREAM),
        ProfileSource.parse(URL, datastream="other.xml"),
    ]
    assert len({source.key for source in sources}) == 3


def test_distinct_fix_languages_may_share_a_remediation_id():
    raw = _create_mock_datastream().replace(
        b"</Benchmark>",
        b'<Rule id="xccdf_example_rule_check"><fix id="shared" system="urn:xccdf:fix:script:sh">true</fix>'
        b'<fix id="shared" system="urn:xccdf:fix:script:ansible">[]</fix></Rule></Benchmark>',
    )
    assert inspect_datastream(raw)["profiles"][0]["id"] == PROFILE
