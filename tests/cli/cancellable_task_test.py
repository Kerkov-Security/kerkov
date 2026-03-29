"""Tests for CancellableTask utility."""

import asyncio
import unittest

from src.cli.cancellable_task import InterruptedByUser, run_cancellable


class RunCancellableTest(unittest.TestCase):
    """Tests for run_cancellable()."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_returns_result_when_coroutine_completes(self):
        async def _work():
            return 42

        event = asyncio.Event()
        result = self._run(run_cancellable(_work(), event))
        self.assertEqual(result, 42)

    def test_raises_interrupted_when_event_set_before_start(self):
        async def _slow():
            await asyncio.sleep(10)

        event = asyncio.Event()
        event.set()

        with self.assertRaises(InterruptedByUser):
            self._run(run_cancellable(_slow(), event))

    def test_raises_interrupted_when_event_set_during_work(self):
        async def _slow():
            await asyncio.sleep(10)

        event = asyncio.Event()

        async def _interrupt_after_delay():
            await asyncio.sleep(0.1)
            event.set()

        async def _run_both():
            asyncio.create_task(_interrupt_after_delay())
            return await run_cancellable(_slow(), event)

        with self.assertRaises(InterruptedByUser):
            self._run(_run_both())

    def test_coroutine_is_cancelled_on_interrupt(self):
        cancelled = False

        async def _work():
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

        event = asyncio.Event()
        event.set()

        with self.assertRaises(InterruptedByUser):
            self._run(run_cancellable(_work(), event))

        self.assertTrue(cancelled)

    def test_propagates_exception_from_coroutine(self):
        async def _fail():
            raise ValueError("boom")

        event = asyncio.Event()

        with self.assertRaises(ValueError):
            self._run(run_cancellable(_fail(), event))


if __name__ == "__main__":
    unittest.main()
