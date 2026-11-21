"""Sign-ins and refreshes coordinate managers that share a state file."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kerkov.core.config_manager import ConfigManager
from kerkov.providers.codex.auth import (
    CodexAuthConfig,
    CodexTokenManager,
    load_codex_state,
    persist_codex_state,
)
from kerkov.providers.codex.oauth import CodexLoginFlow


def _credentials(account):
    return {
        "CODEX_ACCESS_TOKEN": f"{account}-access",
        "CODEX_OAUTH_REFRESH_TOKEN": f"{account}-refresh",
        "CODEX_ACCOUNT_ID": account,
        "CODEX_TOKEN_EXPIRES_AT": "9999999999",
    }


def _create_mock_manager(path, account="old"):
    return CodexTokenManager(
        CodexAuthConfig(path, "https://example.invalid/token", "client", None, 1),
        access_token=f"{account}-access",
        refresh_token=f"{account}-refresh",
        account_id=account,
        expires_at="1",
    )


@pytest.mark.parametrize(
    "field", ["CODEX_ACCESS_TOKEN", "CODEX_OAUTH_REFRESH_TOKEN", "CODEX_ACCOUNT_ID"]
)
@pytest.mark.parametrize("value", [None, "", " ", 123])
def test_invalid_login_cannot_modify_durable_or_live_state(tmp_path, field, value):
    path = tmp_path / "auth.json"
    original = _credentials("old")
    persist_codex_state(path, original)
    manager = _create_mock_manager(path)
    updates = {**_credentials("new"), field: value}
    with pytest.raises(ValueError):
        manager.install_login(updates)
    assert load_codex_state(path) == original
    assert manager.access_token == "old-access"
    assert manager.account_id == "old"
    assert manager.refresh_token == "old-refresh"


def test_stale_tab_adopts_new_login_before_forced_refresh(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    first, stale = _create_mock_manager(path), _create_mock_manager(path)
    first.install_login(_credentials("new"))
    monkeypatch.setattr(
        stale,
        "_refresh_access_token",
        lambda: pytest.fail("stale credentials were refreshed"),
    )
    assert stale.ensure_valid_token(force_refresh=True) == "new-access"
    assert stale.account_id == "new"
    assert stale.refresh_token == "new-refresh"
    assert load_codex_state(path) == _credentials("new")


def test_concurrent_tabs_rotate_the_same_refresh_token_once(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    persist_codex_state(path, {**_credentials("old"), "CODEX_TOKEN_EXPIRES_AT": "1"})
    first, second = _create_mock_manager(path), _create_mock_manager(path)
    entered, release = threading.Event(), threading.Event()
    client = MagicMock()
    client.__enter__.return_value = client

    def post(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        response = MagicMock()
        response.json.return_value = {
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
        }
        return response

    client.post.side_effect = post
    monkeypatch.setattr(
        "kerkov.providers.codex.auth.httpx.Client", lambda **kwargs: client
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            a = pool.submit(first.ensure_valid_token)
            assert entered.wait(2)
            b = pool.submit(second.ensure_valid_token)
            release.set()
            assert a.result(3) == b.result(3) == "rotated-access"
            assert client.post.call_count == 1
            assert second.refresh_token == "rotated-refresh"
        finally:
            release.set()


def test_install_waits_for_old_refresh_and_prevents_later_reversion(
    tmp_path, monkeypatch
):
    path = tmp_path / "auth.json"
    refreshing, signing_in = _create_mock_manager(path), _create_mock_manager(path)
    entered, release = threading.Event(), threading.Event()

    def refresh():
        entered.set()
        assert release.wait(3)
        persist_codex_state(path, _credentials("rotated-old"))
        refreshing.access_token = "rotated-old-access"
        refreshing.refresh_token = "rotated-old-refresh"

    monkeypatch.setattr(refreshing, "_refresh_access_token", refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            old = pool.submit(refreshing.ensure_valid_token)
            assert entered.wait(2)
            install = pool.submit(signing_in.install_login, _credentials("new"))
            assert not install.done()
            release.set()
            old.result(3)
            install.result(3)
        finally:
            release.set()
    assert refreshing.ensure_valid_token(force_refresh=True) == "new-access"
    assert load_codex_state(path) == _credentials("new")


def test_login_does_not_reuse_manager_for_a_different_state_path(tmp_path):
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old = _create_mock_manager(old_path)
    old.install_login(_credentials("old"))
    flow = CodexLoginFlow(ConfigManager(codex_state_path=str(new_path)))
    selected = flow.install(_credentials("new"), old)
    assert selected is not old
    assert selected.state_path == new_path
    assert load_codex_state(old_path) == _credentials("old")
    assert load_codex_state(new_path) == _credentials("new")


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_default_state_directory_is_hardened_when_resolved_from_child(
    tmp_path, monkeypatch
):
    path = tmp_path / "data/providers/codex/auth.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.parent.chmod(0o755)
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    persist_codex_state(path, _credentials("new"))
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_custom_shared_parent_is_not_chmodded(tmp_path):
    tmp_path.chmod(0o755)
    persist_codex_state(tmp_path / "custom.json", _credentials("new"))
    assert tmp_path.stat().st_mode & 0o777 == 0o755
