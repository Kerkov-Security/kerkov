"""WebSocket routes for the web console."""

from __future__ import annotations

from django.urls import path

from kerkov.web.console.consumers import ConsoleConsumer

websocket_urlpatterns = [
    path("ws/console/", ConsoleConsumer.as_asgi()),
]
