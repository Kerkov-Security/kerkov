"""Django app configuration for the web console."""

from __future__ import annotations

from django.apps import AppConfig


class ConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kerkov.web.console"
    label = "kerkov_console"
