"""Stable environment for optional Django web tests."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")
os.environ["KERKOV_WEB_ACCESS_TOKEN"] = "test-web-access-token-0123456789abcdef"
os.environ["KERKOV_WEB_ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1]"
os.environ["KERKOV_WEB_DEBUG"] = "0"
