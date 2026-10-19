"""HTB validation and persistence through the authenticated console socket."""

import json

import httpx
import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.core.credentials import CredentialManager, MemorySecretStore
from kerkov.web.runtime.htb import HTB_TOKEN, HTB_USER_URL
from tests.web.inventory_transport_test import _communicator, _until, runtime


@pytest.fixture
def credentials(monkeypatch):
    manager = CredentialManager(environment={}, persistent_store=MemorySecretStore())
    monkeypatch.delenv("HTB_API_TOKEN", raising=False)
    monkeypatch.setattr(
        "kerkov.web.runtime.session.get_credential_manager", lambda: manager
    )
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"info": {"id": 42, "name": "tester"}}, "htb.configured"),
        (401, {"message": "invalid"}, "htb.error"),
        (403, {"message": "blocked"}, "htb.error"),
        (200, {"message": "not an account"}, "htb.error"),
    ],
)
async def test_verify_and_save(
    runtime, credentials, monkeypatch, status, body, expected
):
    async def get(client, url, **kwargs):
        assert url == HTB_USER_URL
        assert kwargs["headers"]["Authorization"] == "Bearer private-htb-token"
        return httpx.Response(status, json=body)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    socket = _communicator()
    assert (await socket.connect())[0]
    try:
        await _until(socket, "session.ready")
        await socket.send_json_to(
            {
                "type": "htb.configure",
                "request_id": "connect",
                "token": "private-htb-token",
            }
        )
        result = await _until(socket, expected)
        assert result["request_id"] == "connect"
        assert "private-htb-token" not in json.dumps(result)
        assert bool(credentials.new_session().get(HTB_TOKEN)) == (
            expected == "htb.configured"
        )
        assert runtime[0].conversation_history == []
        assert runtime[0].engagement_store.engagement_id is None
        await socket.send_json_to({"type": "htb.status", "request_id": "status"})
        saved = await _until(socket, "htb.status")
        assert saved["configured"] == (expected == "htb.configured")
    finally:
        await socket.disconnect()


@pytest.mark.asyncio
async def test_bad_input_and_environment_override(runtime, credentials, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid inputs must not reach HTB")

    monkeypatch.setattr(httpx.AsyncClient, "get", unexpected)
    socket = _communicator()
    assert (await socket.connect())[0]
    try:
        await _until(socket, "session.ready")
        for token in (None, "", "bad\nheader", "x" * 4097):
            await socket.send_json_to(
                {"type": "htb.configure", "request_id": "invalid", "token": token}
            )
            await _until(socket, "htb.error")
        monkeypatch.setenv("HTB_API_TOKEN", "environment-token")
        await socket.send_json_to(
            {"type": "htb.configure", "request_id": "override", "token": "replacement"}
        )
        result = await _until(socket, "htb.error")
        assert "environment" in result["detail"]
        assert credentials.get(HTB_TOKEN) is None
    finally:
        await socket.disconnect()
