"""Gemini metadata requests retain worker capacity after abandonment."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kerkov.providers import workers
from kerkov.providers.gemini_provider import GeminiProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_abandoned_metadata_keeps_capacity_without_caching_limits(
    monkeypatch, ending
):
    pool = workers.ProviderWorkerPool(max_workers=1)
    monkeypatch.setattr(workers, "_pool", pool)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def metadata(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return SimpleNamespace(input_token_limit=32000, output_token_limit=1024)

    with (
        patch("google.generativeai.configure"),
        patch("google.generativeai.get_model", side_effect=metadata) as lookup,
    ):
        provider = GeminiProvider(api_key="test-key", model="gemini-test")
        task = asyncio.create_task(provider.get_max_context_tokens())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            if ending == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(task, timeout=0.01)
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            # Metadata uses the same pool as validation, without occupying the
            # default executor needed by auth and session initialization.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: True), 1)
            with pytest.raises(workers.ProviderWorkersBusy):
                await provider.get_max_context_tokens()
            lookup.assert_called_once()
            assert not provider._context_window_cache
            assert not provider._max_output_cache
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            pool.shutdown()
        assert not provider._context_window_cache
        assert not provider._max_output_cache
