"""Focused smoke tests for the Django web shell."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

import django
import pytest
from channels.testing import WebsocketCommunicator
from django.test import Client

from kerkov.web import manage
from kerkov.web.project.asgi import application

django.setup()


def test_dashboard_renders() -> None:
    response = Client().get("/")

    assert response.status_code == 200
    assert b"Kerkov Web Console" in response.content
    assert b"Django + Channels" in response.content


def test_health_endpoint() -> None:
    response = Client().get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "kerkov-web"}


def test_runserver_defaults_to_less_common_local_port(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_execute(argv: list[str]) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(manage, "execute_from_command_line", fake_execute)

    manage.main(["kerkov-web", "runserver"])

    assert captured["argv"] == ["kerkov-web", "runserver", "127.0.0.1:8764"]


class _FakeConsumerSession:
    def __init__(self, bus: Any) -> None:
        self.bus = bus

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
        user_message: str | None = None,
    ) -> bool:
        return False

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_console_websocket_round_trip() -> None:
    with patch("kerkov.web.console.consumers.WebKerkovSession", _FakeConsumerSession):
        communicator = WebsocketCommunicator(application, "/ws/console/")

        connected, _ = await communicator.connect()
        assert connected

        ready = await communicator.receive_json_from()
        assert ready["type"] == "session.ready"

        await communicator.send_json_to({"type": "user.message", "message": "hello"})
        assistant_event = await communicator.receive_json_from()

        assert assistant_event["type"] == "assistant.message"
        assert "hello" in assistant_event["payload"]["message"]

        await communicator.disconnect()
