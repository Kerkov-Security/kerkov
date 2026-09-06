"""Cancellable async task utility for user-initiated interrupts."""

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class InterruptedByUser(Exception):
    """Raised when the user presses Escape during an operation."""

    pass


class InterruptHandle(ABC):
    """Cooperative interrupt source — implemented by `InputManager`.

    Subclasses must set `interrupt_requested` to an `asyncio.Event` that
    `run_cancellable` polls while awaiting work.
    """

    interrupt_requested: asyncio.Event

    @abstractmethod
    def start_watching_for_interrupt(self) -> None:
        pass

    @abstractmethod
    def stop_watching_for_interrupt(self) -> None:
        pass

    @abstractmethod
    def reset_interrupt(self) -> None:
        pass


async def _cancel_and_wait(*tasks: "asyncio.Task[Any]") -> None:
    """Cancel tasks and wait for them, so their cleanup runs before we return."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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

    try:
        done, _pending = await asyncio.wait(
            {task, watcher},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        # Our own caller was cancelled (a hard `task.cancel()` on the agent, a
        # web session closing). Nothing else owns these two tasks, so without
        # this the wrapped operation keeps running and lands its side effect
        # long after the run that asked for it is gone.
        await _cancel_and_wait(task, watcher)
        raise

    if task in done:
        await _cancel_and_wait(watcher)
        return task.result()

    # Interrupt fired — cancel the main task, awaiting it so its cleanup
    # handlers run before we raise the higher-level InterruptedByUser.
    await _cancel_and_wait(task, watcher)
    raise InterruptedByUser()


class EventInterrupt(InterruptHandle):
    """An interrupt nobody watches for: the holder sets the event directly.

    `InputManager`'s handle exists to translate a keypress into an event; an
    agent has no terminal, so the supervisor sets the event itself and the
    watch/reset hooks have nothing to do.
    """

    def __init__(self) -> None:
        self.interrupt_requested = asyncio.Event()

    def start_watching_for_interrupt(self) -> None:
        return None

    def stop_watching_for_interrupt(self) -> None:
        return None

    def reset_interrupt(self) -> None:
        self.interrupt_requested.clear()
