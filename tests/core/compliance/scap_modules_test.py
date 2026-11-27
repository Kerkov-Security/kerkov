import asyncio
import hashlib
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from kerkov.core.compliance.scap_modules import (
    SCAP_MODULES,
    ScapModule,
    stage_scap_modules,
    verify_scap_modules,
)


def test_pinned_module_is_downloaded_once_and_staged_per_run(tmp_path, monkeypatch):
    name = "ansible.posix.sysctl"
    data = b"# local fixture"
    source = ScapModule("1.0.0", "a" * 40, hashlib.sha256(data).hexdigest())
    monkeypatch.setitem(SCAP_MODULES, name, source)
    requests = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, content=data)

    transport = httpx.MockTransport(serve)
    cache, preview, apply = (tmp_path / name for name in ("cache", "preview", "apply"))
    client = httpx.AsyncClient
    with patch(
        "kerkov.core.compliance.scap_modules.httpx.AsyncClient",
        side_effect=lambda **kwargs: client(transport=transport, **kwargs),
    ):
        asyncio.run(stage_scap_modules(cache, preview, [name]))
        asyncio.run(stage_scap_modules(cache, apply, [name]))
    assert len(requests) == 1
    assert requests[0].host == "raw.githubusercontent.com"
    assert requests[0].path == (
        f"/ansible-collections/ansible.posix/{source.commit}/plugins/modules/sysctl.py"
    )
    local_path = Path(
        "collections/ansible_collections/ansible/posix/plugins/modules/sysctl.py"
    )
    assert (
        (preview / local_path).read_bytes() == (apply / local_path).read_bytes() == data
    )
    (apply / local_path).with_name("unexpected.py").touch()
    with pytest.raises(ValueError, match="directory changed"):
        verify_scap_modules(apply, [name])


def test_pinned_module_rejects_unverified_bytes(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"wrong")
    )
    client = httpx.AsyncClient
    preview = tmp_path / "preview"
    cache = tmp_path / "cache"
    with (
        patch(
            "kerkov.core.compliance.scap_modules.httpx.AsyncClient",
            side_effect=lambda **kwargs: client(transport=transport, **kwargs),
        ),
        pytest.raises(ValueError, match="failed SHA-256 verification"),
    ):
        asyncio.run(stage_scap_modules(cache, preview, ["ansible.posix.sysctl"]))
    assert not cache.exists()
    assert not preview.exists()
