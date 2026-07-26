"""HTTP views for the Kerkov web console."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

from kerkov.cli.modes import AUDIT_MODE, CTF_MODE, OFFENSIVE_MODE, RECON_MODE


def dashboard(request: HttpRequest) -> HttpResponse:
    """Render the first web console screen."""

    modes = [RECON_MODE, AUDIT_MODE, OFFENSIVE_MODE, CTF_MODE]
    response = render(
        request,
        "console/dashboard.html",
        {
            "modes": modes,
            "default_mode": AUDIT_MODE,
        },
    )
    response["Cache-Control"] = "no-store"
    response.set_cookie(
        settings.KERKOV_WEB_ACCESS_COOKIE_NAME,
        settings.KERKOV_WEB_ACCESS_TOKEN,
        httponly=True,
        samesite="Strict",
        path="/ws/console/",
    )
    return response


def health(request: HttpRequest) -> JsonResponse:
    """Cheap health endpoint for smoke checks."""

    return JsonResponse({"status": "ok", "service": "kerkov-web"})
