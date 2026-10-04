"""Provider setup, defaults, and activation over the real local socket contract."""

import json

import pytest

pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from kerkov.core.credentials import GEMINI_API_KEY, CredentialManager, MemorySecretStore
from tests.web.inventory_transport_test import _communicator, _until, runtime
from tests.web.provider_runtime_test import FakeProvider


@pytest.mark.asyncio
async def test_setup_test_save_default_then_activate(runtime, tmp_path, monkeypatch):
    credentials = CredentialManager(
        environment={}, persistent_store=MemorySecretStore()
    )
    monkeypatch.setattr(
        "kerkov.web.runtime.session.get_credential_manager", lambda: credentials
    )
    settings_file = tmp_path / "settings.json"
    monkeypatch.setenv("KERKOV_SETTINGS_FILE", str(settings_file))
    candidates = []

    def create(config, name, candidate_credentials):
        assert name == "gemini"
        assert candidate_credentials.get(GEMINI_API_KEY) == "new-test-credential"
        candidate = FakeProvider("GeminiProvider", "old-model")
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(
        "kerkov.web.runtime.provider_settings.create_provider_from_name", create
    )
    monkeypatch.setattr(
        "kerkov.web.runtime.providers.create_provider_from_name", create
    )
    communicator = _communicator()
    assert (await communicator.connect())[0]
    try:
        await _until(communicator, "session.ready")
        await communicator.send_json_to(
            {
                "type": "provider.test",
                "request_id": "test",
                "provider": "gemini",
                "api_key": "new-test-credential",
            }
        )
        tested = await _until(communicator, "provider.tested")
        assert tested["request_id"] == "test"
        assert tested["models"] == ["new-model", "old-model"]
        assert "new-test-credential" not in json.dumps(tested)
        assert credentials.get(GEMINI_API_KEY) is None
        assert runtime[0].provider is None

        await communicator.send_json_to(
            {
                "type": "provider.configure",
                "request_id": "save",
                "provider": "gemini",
                "api_key": "new-test-credential",
                "persist": False,
            }
        )
        configured = await _until(communicator, "provider.configured")
        assert configured["persisted"] is False
        assert runtime[0].credentials.get(GEMINI_API_KEY) == "new-test-credential"
        assert credentials.get(GEMINI_API_KEY) is None
        assert runtime[0].provider is None
        assert runtime[0].engagement_store.engagement_id is None

        await communicator.send_json_to(
            {
                "type": "provider.default",
                "request_id": "default",
                "provider": "gemini",
                "model": "new-model",
            }
        )
        saved = await _until(communicator, "provider.default_saved")
        assert saved["default"] == {"provider": "gemini", "model": "new-model"}
        assert json.loads(settings_file.read_text()) == {
            "LLM_PROVIDER": "gemini",
            "DEFAULT_GEMINI_MODEL": "new-model",
        }
        assert runtime[0].provider is None

        await communicator.send_json_to(
            {
                "type": "provider.switch",
                "request_id": "activate",
                "provider": "gemini",
                "model": "new-model",
            }
        )
        changed = await _until(communicator, "provider.changed")
        assert changed["active"] == saved["default"]
        assert runtime[0].provider is candidates[-1]
        assert not any(candidate.requests for candidate in candidates)
    finally:
        await communicator.disconnect()
    assert runtime[0].credentials.get(GEMINI_API_KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [False, True])
async def test_websocket_credentials_are_private_unless_persisted(
    runtime, monkeypatch, persist
):
    shared = CredentialManager(environment={}, persistent_store=MemorySecretStore())
    monkeypatch.setattr(
        "kerkov.web.runtime.session.get_credential_manager", lambda: shared
    )
    monkeypatch.setattr(
        "kerkov.web.runtime.provider_settings.create_provider_from_name",
        lambda *args: FakeProvider("GeminiProvider", "old-model"),
    )
    first, other = _communicator(), _communicator()
    assert (await first.connect())[0]
    assert (await other.connect())[0]
    try:
        await _until(first, "session.ready")
        await _until(other, "session.ready")
        await first.send_json_to(
            {
                "type": "provider.configure",
                "request_id": "save",
                "provider": "gemini",
                "api_key": "socket-private-key",
                "persist": persist,
            }
        )
        saved = await _until(first, "provider.configured")
        assert saved["persisted"] is persist
        assert runtime[0].credentials is not runtime[1].credentials
        assert runtime[0].credentials.get(GEMINI_API_KEY) == "socket-private-key"
        expected = "socket-private-key" if persist else None
        assert shared.get(GEMINI_API_KEY) == expected
        assert runtime[1].credentials.get(GEMINI_API_KEY) == expected
    finally:
        await first.disconnect()
        await other.disconnect()
    assert runtime[0].credentials.get(GEMINI_API_KEY) == expected
    reopened = _communicator()
    assert (await reopened.connect())[0]
    try:
        await _until(reopened, "session.ready")
        assert runtime[-1].credentials.get(GEMINI_API_KEY) == expected
    finally:
        await reopened.disconnect()
