"""Community fix imports verify provenance and keep review local."""

import asyncio
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kerkov.core.compliance.community_fixes import (
    CommunityFixStore,
    community_fix_catalog,
)


def _create_mock_devsec_archive():
    artifact = io.BytesIO()
    with tarfile.open(fileobj=artifact, mode="w:gz") as bundle:
        for role in ("os_hardening", "ssh_hardening", "nginx_hardening"):
            data = b"[]\n"
            info = tarfile.TarInfo(f"release/roles/{role}/tasks/main.yml")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return artifact.getvalue()


def test_import_one_devsec_role_exposes_all_pinned_roles_and_checks_bundle(tmp_path):
    store = CommunityFixStore(tmp_path)
    archive = _create_mock_devsec_archive()
    source = next(
        item for item in community_fix_catalog() if item["id"] == "os_hardening"
    )
    with patch(
        "kerkov.core.compliance.community_fixes.GitHubProfiles.download",
        new=AsyncMock(return_value=archive),
    ) as download:
        assert store.list() == []
        assert set(asyncio.run(store.import_fix("os_hardening"))) == {
            "os_hardening",
            "ssh_hardening",
            "nginx_hardening",
        }
        download.assert_awaited_once_with(
            {"url": source["url"], "commit": source["revision"]}
        )
        assert "nginx_hardening" in asyncio.run(store.import_fix("nginx_hardening"))
        assert download.await_count == 1
        reviewed = store.review("ssh_hardening")
        tasks = json.loads(reviewed["preview"])[0]["tasks"]
        assert tasks == [
            {"ansible.builtin.include_role": {"name": "devsec.hardening.ssh_hardening"}}
        ]
        assert reviewed["scope"] == "Whole role"
        role_tasks = Path(reviewed["role_tasks"])
        role_tasks.write_bytes(b"changed")
        with pytest.raises(ValueError, match="bundle changed"):
            store.review("ssh_hardening")
        asyncio.run(store.import_fix("ssh_hardening"))
        assert download.await_count == 2
        assert store.review("ssh_hardening")["role_tasks"] != str(role_tasks)


def test_curated_id_required_before_network_or_file_changes(tmp_path):
    store = CommunityFixStore(tmp_path)
    with patch(
        "kerkov.core.compliance.community_fixes.GitHubProfiles.download",
        new=AsyncMock(),
    ) as download:
        with pytest.raises(ValueError, match="community library"):
            asyncio.run(store.import_fix("https://github.com/unreviewed/project"))
        download.assert_not_awaited()
    assert not list(tmp_path.iterdir())
