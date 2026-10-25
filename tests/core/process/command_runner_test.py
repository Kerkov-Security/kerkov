"""Cancellation during and after spawning must not orphan local workers."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kerkov.core.process.command_runner import SubprocessCommandRunner


class CommandRunnerTest(unittest.TestCase):
    def test_cancellation_reaps_an_active_process(self):
        self._exercise(False)

    def test_cancellation_during_spawn_kills_a_late_child(self):
        self._exercise(True)

    def _exercise(self, late):
        async def exercise(root):
            created = []
            create = asyncio.create_subprocess_exec
            started = asyncio.Event()
            release = asyncio.Event()

            async def capture(*args, **kwargs):
                process = await create(*args, **kwargs)
                created.append(process)
                started.set()
                if late:
                    await release.wait()
                return process

            with patch(
                "kerkov.core.process.command_runner.asyncio.create_subprocess_exec",
                side_effect=capture,
            ):
                task = asyncio.create_task(
                    SubprocessCommandRunner().run(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        cwd=root,
                        timeout_seconds=60,
                        log_path=root / "run.log",
                        environment={},
                        display_name="test command",
                    )
                )
                try:
                    await asyncio.wait_for(started.wait(), timeout=5)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    release.set()
                    await asyncio.wait_for(created[0].wait(), timeout=5)
                    self.assertIsNotNone(created[0].returncode)
                finally:
                    if created and created[0].returncode is None:
                        created[0].kill()
                        await created[0].wait()

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(exercise(Path(temp_dir)))
