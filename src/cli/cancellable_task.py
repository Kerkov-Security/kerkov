"""Cancellable async task utility for user-initiated interrupts."""

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

class InterruptedByUser(Exception):
    """Raised when the user presses Escape during an operation."""

    pass

async def run_cancellable(
    coro: Coroutine[Any, Any, T],
    interrupt_event: asyncio.Event,
) -> T:
    """Run a coroutine, cancelling it if interrupt_event is set.

    Args:
        coro: The coroutine to run.
        interrupt_event: An asyncio.Event that signals user interruption.

    Returns:
        The result of the coroutine if it completes normally.

    Raises:
        InterruptedByUser: If the interrupt event fires before completion.
    """
    task = asyncio.create_task(coro)

    async def _watch_interrupt() -> None:
        while not interrupt_event.is_set():
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(_watch_interrupt())

    done, pending = await asyncio.wait(
        {task, watcher},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if task in done:
        watcher.cancel()
        return task.result()

    # Interrupt fired — cancel the main task
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    watcher.cancel()
    raise InterruptedByUser()
