"""Provider capacity tracks real SDK work across canceled async requests."""

import asyncio
import contextvars
import gc
import threading
from unittest.mock import Mock

import pytest

from kerkov.providers import workers
from kerkov.providers.workers import ProviderWorkerPool, ProviderWorkersBusy


@pytest.fixture
def pool(monkeypatch):
    pool = ProviderWorkerPool(max_workers=1)
    monkeypatch.setattr(workers, "_pool", pool)
    yield pool
    pool.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "cancel"])
@pytest.mark.parametrize("raises", [False, True])
async def test_abandoned_call_keeps_global_capacity_until_real_exit(
    pool, monkeypatch, ending, raises
):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    loop_errors = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: loop_errors.append(context))
    futures = []
    submit = pool._executor.submit

    def capture(*args, **kwargs):
        future = submit(*args, **kwargs)
        futures.append(future)
        return future

    monkeypatch.setattr(pool._executor, "submit", capture)

    def blocked():
        assert threading.current_thread().name.startswith("kerkov-provider")
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        if raises:
            raise RuntimeError("private-provider-secret")
        return "finished"

    task = asyncio.create_task(workers.run_provider_worker(blocked))
    try:
        await asyncio.wait_for(started.wait(), 1)
        # Register after run() attached its capacity-release callback.
        futures[0].add_done_callback(lambda _: loop.call_soon_threadsafe(finished.set))
        if ending == "timeout":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, 0.01)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        unwanted = Mock()
        for _ in range(5):
            with pytest.raises(ProviderWorkersBusy, match="Wait and retry"):
                await workers.run_provider_worker(unwanted)
        assert len(futures) == 1
        unwanted.assert_not_called()
        # A different session/event loop shares the same process capacity.
        with pytest.raises(ProviderWorkersBusy):
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: asyncio.run(workers.run_provider_worker(unwanted))
                ),
                1,
            )
        # Default-executor work remains available while provider work is stuck.
        assert (
            await asyncio.wait_for(asyncio.to_thread(lambda: "responsive"), 1)
            == "responsive"
        )
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert await workers.run_provider_worker(lambda: "recovered") == "recovered"
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert loop_errors == []
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_worker_preserves_context_and_reclaims_capacity_after_failure(pool):
    current_request = contextvars.ContextVar("provider-request", default="unset")
    current_request.set("request-1")

    def check(*, expected):
        assert current_request.get() == expected
        raise ValueError("worker failed")

    with pytest.raises(ValueError, match="worker failed"):
        await workers.run_provider_worker(check, expected="request-1")
    assert await workers.run_provider_worker(current_request.get) == "request-1"


@pytest.mark.asyncio
async def test_submit_failure_returns_admission_capacity(pool, monkeypatch):
    submit = pool._executor.submit
    monkeypatch.setattr(
        pool._executor, "submit", Mock(side_effect=RuntimeError("submit failed"))
    )
    with pytest.raises(RuntimeError, match="submit failed"):
        await workers.run_provider_worker(lambda: None)
    monkeypatch.setattr(pool._executor, "submit", submit)
    assert await workers.run_provider_worker(lambda: "recovered") == "recovered"
