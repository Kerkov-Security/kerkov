"""URL routes for the Kerkov web app."""

from __future__ import annotations

from django.urls import path

from kerkov.web.console import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
]
