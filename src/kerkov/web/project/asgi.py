"""ASGI application for HTTP and WebSocket traffic."""

from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

from kerkov.web.console.routing import websocket_urlpatterns

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
