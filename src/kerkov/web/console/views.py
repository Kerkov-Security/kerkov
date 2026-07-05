"""HTTP views for the Kerkov web console."""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.shortcuts import render

from kerkov.cli.modes import AUDIT_MODE, CTF_MODE, OFFENSIVE_MODE, RECON_MODE


def dashboard(request: HttpRequest):
    """Render the first web console screen."""

    modes = [RECON_MODE, AUDIT_MODE, OFFENSIVE_MODE, CTF_MODE]
    return render(
        request,
        "console/dashboard.html",
        {
            "modes": modes,
            "default_mode": AUDIT_MODE,
        },
    )


def health(request: HttpRequest) -> JsonResponse:
    """Cheap health endpoint for smoke checks."""

    return JsonResponse({"status": "ok", "service": "kerkov-web"})
