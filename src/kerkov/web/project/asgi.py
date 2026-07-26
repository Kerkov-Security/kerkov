"""ASGI application for HTTP and WebSocket traffic."""

from __future__ import annotations

import os

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from channels.sessions import CookieMiddleware
from django.core.asgi import get_asgi_application

from kerkov.web.console.routing import websocket_urlpatterns
from kerkov.web.console.security import SameOriginWebSocketValidator

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": SameOriginWebSocketValidator(
            AllowedHostsOriginValidator(
                CookieMiddleware(URLRouter(websocket_urlpatterns))
            )
        ),
    }
)
