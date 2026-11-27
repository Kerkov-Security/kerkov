"""Checks for the pinned OpenSCAP release cache."""

import asyncio
import hashlib
import io
import zipfile

import httpx
import pytest

from kerkov.core.compliance import scap_catalog


def _create_mock_release(streams, playbook):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for preset, raw in streams:
            bundle.writestr(f"{scap_catalog._RELEASE_NAME}/{preset.filename}", raw)
        bundle.writestr(
            f"{scap_catalog._RELEASE_NAME}/ansible/{scap_catalog.scap_playbook_name(9)}",
            playbook,
        )
    return archive.getvalue()


def test_concurrent_profiles_share_one_release_and_cache_is_reverified(
    tmp_path, monkeypatch
):
    streams = [
        (scap_catalog.ScapPreset(major, hashlib.sha256(raw).hexdigest()), raw)
        for major, raw in [(8, b"rhel8"), (9, b"rhel9"), (10, b"rhel10")]
    ]
    playbook = b"fixture remediation"
    archive = _create_mock_release(streams, playbook)
    monkeypatch.setattr(
        scap_catalog, "_FIX_FILES", {9: hashlib.sha256(playbook).hexdigest()}
    )
    monkeypatch.setattr(
        scap_catalog, "_ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest()
    )
    monkeypatch.setattr(
        scap_catalog, "_PRESETS", {item.id: item for item, _ in streams}
    )
    requests = []

    async def download(request):
        requests.append(str(request.url))
        await asyncio.sleep(0.03)
        return httpx.Response(200, content=archive)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        scap_catalog.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(download), **kwargs),
    )

    async def verify():
        paths = await asyncio.gather(
            *(
                scap_catalog.prepare_scap_preset(tmp_path, preset)
                for preset, _ in streams
            )
        )
        assert [path.read_bytes() for path in paths] == [raw for _, raw in streams]
        assert requests == [scap_catalog._ARCHIVE_URL]
        fix = await scap_catalog.prepare_scap_playbook(tmp_path, 9)
        assert fix.read_bytes() == playbook
        assert requests == [scap_catalog._ARCHIVE_URL]
        paths[0].write_bytes(b"changed")
        restored = await scap_catalog.prepare_scap_preset(tmp_path, streams[0][0])
        assert restored.read_bytes() == streams[0][1]
        assert requests == [scap_catalog._ARCHIVE_URL] * 2
        assert not list(restored.parent.glob(".stage-*"))
        assert not list(restored.parent.glob("*.zip"))

    asyncio.run(verify())


def test_wrong_release_digest_never_publishes_a_datastream(tmp_path, monkeypatch):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        scap_catalog.httpx,
        "AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"unexpected release")
            ),
            **kwargs,
        ),
    )
    preset = next(iter(scap_catalog._PRESETS.values()))
    with pytest.raises(ValueError, match="reviewed SHA-256"):
        asyncio.run(scap_catalog.prepare_scap_preset(tmp_path, preset))
    assert not list(tmp_path.rglob("*.xml"))
    assert not list(tmp_path.rglob(".stage-*"))
