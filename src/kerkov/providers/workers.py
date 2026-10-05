"""Bound blocking provider calls independently of their async request lifetime."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, ParamSpec, TypeVar

MAX_PROVIDER_WORKERS = 4
P = ParamSpec("P")
T = TypeVar("T")


class ProviderWorkersBusy(RuntimeError):
    """A literal, safe-to-display capacity error without provider details."""

    def __init__(self) -> None:
        super().__init__("Provider checks are still running. Wait and retry.")


class ProviderWorkerPool:
    """A dedicated executor whose real outstanding work has a strict bound.

    An SDK call may outlive an asyncio timeout or disconnect. Its capacity is
    returned by the concurrent future, never by the canceled waiter. The limit
    also covers work waiting to start, so the executor cannot build an unbounded
    queue. A threading semaphore keeps admission shared across event loops.
    """

    def __init__(self, max_workers: int = MAX_PROVIDER_WORKERS) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kerkov-provider"
        )
        self._capacity = threading.BoundedSemaphore(max_workers)

    async def run(
        self, function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        if not self._capacity.acquire(blocking=False):
            raise ProviderWorkersBusy()
        try:
            context = contextvars.copy_context()
            future = self._executor.submit(context.run, function, *args, **kwargs)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda _: self._capacity.release())
        # Canceling this await can cancel work that has not started. A running
        # call continues to own its slot, and its late result/exception cannot
        # resurrect the canceled request or become an unhandled asyncio error.
        return await asyncio.wrap_future(future)

    def shutdown(self) -> None:
        """Drain the executor when its owning process/test is shutting down."""
        self._executor.shutdown(wait=True, cancel_futures=True)


_pool = ProviderWorkerPool()


async def run_provider_worker(
    function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run a blocking provider call within the process-wide capacity limit."""
    return await _pool.run(function, *args, **kwargs)
