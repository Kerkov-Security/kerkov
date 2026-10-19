"""Stable environment for optional Django web tests."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kerkov.web.project.settings")
os.environ["KERKOV_WEB_ACCESS_TOKEN"] = "test-web-access-token-0123456789abcdef"
os.environ["KERKOV_WEB_ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1]"
os.environ["KERKOV_WEB_DEBUG"] = "0"


def pytest_collection_finish(session: pytest.Session) -> None:
    """Undo daphne's import-time selector loop policy on Windows."""
    # django.setup() in the browser tests imports daphne, which installs a
    # process-wide selector policy that cannot spawn the exec tests' processes.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


@pytest.fixture(autouse=True)
def isolated_engagement_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep engagement JSONL and its knowledge base out of the real ~/.kerkov.

    `WebKerkovSession` builds its own `EngagementStore` with no seam for a
    directory, so a web test that runs a turn would otherwise write into the
    developer's own engagement history.
    """
    from kerkov.core.engagement import engagement_store

    monkeypatch.setattr(engagement_store, "DEFAULT_STORE_DIR", tmp_path / "engagements")
