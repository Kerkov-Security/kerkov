"""Focused smoke tests for the Django web shell."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from typing import Any, Optional
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

import pytest

django = pytest.importorskip("django")
pytest.importorskip("channels")
pytest.importorskip("daphne")

from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.test import Client

from kerkov.web import manage
from kerkov.web.project.asgi import application

django.setup()


def test_dashboard_renders() -> None:
    response = Client().get("/", HTTP_HOST="localhost")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert b"Kerkov Web Console" in response.content
    assert b"Django + Channels" in response.content
    assert b"Under development." in response.content
    assert b"may be buggy" in response.content
    assert b"local-only and unauthenticated" in response.content
    assert b'data-mode="audit"' in response.content
    assert b'aria-pressed="true"' in response.content
    assert b'id="send-button" type="submit" disabled' in response.content
    assert settings.KERKOV_WEB_ACCESS_TOKEN.encode() not in response.content
    cookie = response.cookies[settings.KERKOV_WEB_ACCESS_COOKIE_NAME]
    assert cookie.value == settings.KERKOV_WEB_ACCESS_TOKEN
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Strict"
    assert cookie["path"] == "/ws/console/"


def test_health_endpoint() -> None:
    response = Client().get("/health/", HTTP_HOST="localhost")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "kerkov-web"}


def test_runserver_defaults_to_less_common_local_port(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_execute(argv: list[str]) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(manage, "execute_from_command_line", fake_execute)

    manage.main(["kerkov-web", "runserver"])

    assert captured["argv"] == [
        "kerkov-web",
        "runserver",
        "127.0.0.1:8764",
        "--insecure",
    ]


def test_runserver_rejects_non_loopback_bind(monkeypatch) -> None:
    execute = patch.object(manage, "execute_from_command_line")
    with execute as fake_execute, pytest.raises(SystemExit, match="localhost"):
        manage.main(["kerkov-web", "runserver", "0.0.0.0:8764"])

    fake_execute.assert_not_called()


def test_runserver_rejects_non_loopback_bind_after_option_value(monkeypatch) -> None:
    execute = patch.object(manage, "execute_from_command_line")
    with execute as fake_execute, pytest.raises(SystemExit, match="localhost"):
        manage.main(
            [
                "kerkov-web",
                "runserver",
                "--verbosity",
                "2",
                "0.0.0.0:8764",
            ]
        )

    fake_execute.assert_not_called()


def test_runserver_allows_ipv6_loopback(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        manage,
        "execute_from_command_line",
        lambda argv: captured.setdefault("argv", argv),
    )

    manage.main(["kerkov-web", "runserver", "[::1]:8764"])

    assert captured["argv"] == [
        "kerkov-web",
        "runserver",
        "[::1]:8764",
        "--insecure",
    ]


def test_runserver_preserves_daphne_options(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        manage,
        "execute_from_command_line",
        lambda argv: captured.setdefault("argv", argv),
    )

    argv = [
        "kerkov-web",
        "runserver",
        "--http_timeout",
        "30",
        "127.0.0.1:8764",
    ]
    manage.main(argv)

    assert captured["argv"] == [*argv, "--insecure"]


def test_runserver_debug_mode_does_not_add_insecure(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        manage,
        "execute_from_command_line",
        lambda argv: captured.setdefault("argv", argv),
    )

    with patch.object(manage.settings, "DEBUG", True):
        manage.main(["kerkov-web", "runserver"])

    assert captured["argv"] == ["kerkov-web", "runserver", "127.0.0.1:8764"]


def test_runserver_preserves_explicit_static_options(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        manage,
        "execute_from_command_line",
        lambda argv: captured.setdefault("argv", argv),
    )

    manage.main(["kerkov-web", "runserver", "--nostatic"])

    assert captured["argv"] == [
        "kerkov-web",
        "runserver",
        "--nostatic",
        "127.0.0.1:8764",
    ]


def test_main_loads_dotenv_before_django_setup(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        manage, "find_dotenv", lambda **kwargs: calls.append("find") or "/tmp/.env"
    )
    monkeypatch.setattr(
        manage, "load_dotenv", lambda path: calls.append(f"load:{path}")
    )
    monkeypatch.setattr(manage, "django_setup", lambda: calls.append("setup"))
    monkeypatch.setattr(
        manage,
        "execute_from_command_line",
        lambda argv: calls.append(f"execute:{argv[1]}"),
    )

    manage.main(["kerkov-web", "check"])

    assert calls == ["find", "load:/tmp/.env", "setup", "execute:check"]


def test_load_web_environment_from_working_directory(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("KERKOV_WEB_TEST_SENTINEL=loaded-from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KERKOV_WEB_TEST_SENTINEL", raising=False)

    manage._load_web_environment()

    assert os.environ["KERKOV_WEB_TEST_SENTINEL"] == "loaded-from-dotenv"


def test_settings_reject_non_ascii_access_token() -> None:
    env = dict(os.environ)
    env["KERKOV_WEB_ACCESS_TOKEN"] = "tökén"
    result = subprocess.run(
        [sys.executable, "-c", "import kerkov.web.project.settings"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode != 0
    assert "must contain only ASCII characters" in result.stderr


class _FakeConsumerSession:
    constructor_thread_id: Optional[int] = None

    def __init__(self, bus: Any) -> None:
        self.bus = bus
        type(self).constructor_thread_id = threading.get_ident()

    async def initialize(self) -> None:
        self.bus.emit(
            "session.ready",
            {
                "title": "Test web console online",
                "provider_ready": True,
                "approval_mode": "normal",
            },
        )

    async def handle_user_message(self, message: str) -> None:
        self.bus.emit("assistant.message", {"message": f"echo: {message}"})

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        user_message: Optional[str] = None,
    ) -> bool:
        return False

    def close(self) -> None:
        pass


def _websocket_path() -> str:
    return "/ws/console/"


def _allowed_headers(
    token: Optional[str] = None,
    *,
    origin: str = "http://localhost:8764",
    host: str = "localhost:8764",
) -> list[tuple[bytes, bytes]]:
    access_token = token if token is not None else settings.KERKOV_WEB_ACCESS_TOKEN
    cookie = f"{settings.KERKOV_WEB_ACCESS_COOKIE_NAME}={access_token}".encode("ascii")
    return [
        (b"host", host.encode("ascii")),
        (b"origin", origin.encode("ascii")),
        (b"cookie", cookie),
    ]


def _communicator(headers: list[tuple[bytes, bytes]]) -> WebsocketCommunicator:
    return WebsocketCommunicator(
        application,
        _websocket_path(),
        headers=headers,
    )


@pytest.mark.asyncio
async def test_console_websocket_round_trip() -> None:
    with patch("kerkov.web.console.consumers.WebKerkovSession", _FakeConsumerSession):
        communicator = _communicator(_allowed_headers())

        connected, _ = await communicator.connect()
        assert connected
        assert _FakeConsumerSession.constructor_thread_id != threading.get_ident()

        ready = await communicator.receive_json_from()
        assert ready["type"] == "session.ready"

        await communicator.send_json_to(
            {"type": "user.message", "message_id": "message-1", "message": "hello"}
        )
        accepted = await communicator.receive_json_from()
        assistant_event = await communicator.receive_json_from()
        complete = await communicator.receive_json_from()

        assert accepted["type"] == "user.message.accepted"
        assert assistant_event["type"] == "assistant.message"
        assert "hello" in assistant_event["payload"]["message"]
        assert complete["type"] == "user.message.complete"

        await communicator.disconnect()


@pytest.mark.asyncio
async def test_console_websocket_rejects_hostile_origin_before_session_creation() -> (
    None
):
    with patch("kerkov.web.console.consumers.WebKerkovSession") as session_class:
        communicator = _communicator(_allowed_headers(origin="https://evil.example"))

        connected, _ = await communicator.connect()

        assert connected is False
        session_class.assert_not_called()


@pytest.mark.asyncio
async def test_console_websocket_rejects_missing_origin() -> None:
    headers = [
        header for header in _allowed_headers() if header[0].lower() != b"origin"
    ]
    communicator = _communicator(headers)

    connected, _ = await communicator.connect()

    assert connected is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:9999",
        "https://localhost:8764",
        "http://127.0.0.1:8764",
    ],
)
@pytest.mark.asyncio
async def test_console_websocket_rejects_nonmatching_origin_authority(
    origin: str,
) -> None:
    with patch("kerkov.web.console.consumers.WebKerkovSession") as session_class:
        communicator = _communicator(_allowed_headers(origin=origin))

        connected, _ = await communicator.connect()

        assert connected is False
        session_class.assert_not_called()


@pytest.mark.asyncio
async def test_console_websocket_rejects_invalid_access_token() -> None:
    communicator = _communicator(_allowed_headers("invalid-token"))

    connected, _ = await communicator.connect()

    assert connected is False


@pytest.mark.asyncio
async def test_console_websocket_rejects_missing_access_cookie() -> None:
    headers = [
        header for header in _allowed_headers() if header[0].lower() != b"cookie"
    ]
    communicator = _communicator(headers)

    connected, _ = await communicator.connect()

    assert connected is False


@pytest.mark.asyncio
async def test_console_websocket_validates_message_type_and_length() -> None:
    with patch("kerkov.web.console.consumers.WebKerkovSession", _FakeConsumerSession):
        communicator = _communicator(_allowed_headers())
        connected, _ = await communicator.connect()
        assert connected
        await communicator.receive_json_from()

        await communicator.send_json_to(
            {"type": "user.message", "message_id": "bad-type", "message": {}}
        )
        bad_type = await communicator.receive_json_from()

        await communicator.send_json_to(
            {
                "type": "user.message",
                "message_id": "too-long",
                "message": "x" * 32_001,
            }
        )
        too_long = await communicator.receive_json_from()

        await communicator.send_json_to(
            {
                "type": "user.message",
                "message_id": "whitespace-padding",
                "message": (" " * 32_000) + "x",
            }
        )
        whitespace_padding = await communicator.receive_json_from()

        await communicator.send_json_to(
            {"type": "user.message", "message_id": "", "message": "hello"}
        )
        empty_id = await communicator.receive_json_from()

        assert bad_type["type"] == "user.message.rejected"
        assert "string" in bad_type["payload"]["detail"]
        assert too_long["type"] == "user.message.rejected"
        assert "limit" in too_long["payload"]["detail"]
        assert whitespace_padding["type"] == "user.message.rejected"
        assert "limit" in whitespace_padding["payload"]["detail"]
        assert empty_id["type"] == "user.message.rejected"
        assert "ID" in empty_id["payload"]["detail"]

        await communicator.disconnect()


class _BlockingConsumerSession(_FakeConsumerSession):
    instances: list["_BlockingConsumerSession"] = []

    def __init__(self, bus: Any) -> None:
        super().__init__(bus)
        self.started = asyncio.Event()
        self.cancelled = False
        self.closed = False
        self.instances.append(self)

    async def handle_user_message(self, message: str) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_console_disconnect_closes_session_and_awaits_message_cancellation() -> (
    None
):
    _BlockingConsumerSession.instances.clear()
    with patch(
        "kerkov.web.console.consumers.WebKerkovSession", _BlockingConsumerSession
    ):
        communicator = _communicator(_allowed_headers())
        connected, _ = await communicator.connect()
        assert connected
        await communicator.receive_json_from()

        await communicator.send_json_to(
            {"type": "user.message", "message_id": "slow", "message": "wait"}
        )
        accepted = await communicator.receive_json_from()
        assert accepted["type"] == "user.message.accepted"
        session = _BlockingConsumerSession.instances[-1]
        await asyncio.wait_for(session.started.wait(), timeout=1)

        await communicator.disconnect()

        assert session.closed is True
        assert session.cancelled is True
