import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path.cwd() / 'src'))


class BrowserLoop:
    def pytest_collection_finish(self, session):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


raise SystemExit(pytest.main(sys.argv[1:], plugins=[BrowserLoop()]))
