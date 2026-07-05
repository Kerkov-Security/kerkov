"""Django settings for the local Kerkov web UI."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
BASE_DIR = PACKAGE_DIR.parents[2]

SECRET_KEY = os.getenv(
    "KERKOV_WEB_SECRET_KEY",
    "kerkov-dev-secret-key-change-before-hosting",
)
DEBUG = os.getenv("KERKOV_WEB_DEBUG", "1").lower() not in {"0", "false", "no"}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "KERKOV_WEB_ALLOWED_HOSTS",
        "127.0.0.1,localhost,[::1],testserver",
    ).split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.staticfiles",
    "kerkov.web.console",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kerkov.web.project.urls"
ASGI_APPLICATION = "kerkov.web.project.asgi.application"
WSGI_APPLICATION = "kerkov.web.project.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    }
]

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}
