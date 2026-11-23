"""Subprocess support follows the loop's capability, not the platform."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kerkov.core.process.command_runner import SubprocessCommandRunner
from kerkov.core.process.subprocess_support import (
    ensure_subprocess_support,
    loop_supports_subprocesses,
)

ONE_MIB = 1024 * 1024


class _StubSubprocessLoop(asyncio.SelectorEventLoop):
    _make_subprocess_transport = asyncio.BaseEventLoop._make_subprocess_transport


@pytest.fixture
def threaded_loop():
    loop = _StubSubprocessLoop()
    assert ensure_subprocess_support(loop)
    try:
        yield loop
    finally:
        loop.close()


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_default_loop_supports_subprocesses_and_is_left_untouched():
    loop = asyncio.new_event_loop()
    try:
        assert loop_supports_subprocesses(loop)
        assert not ensure_subprocess_support(loop)
        assert "_make_subprocess_transport" not in vars(loop)
    finally:
        loop.close()


def test_loop_with_unimplemented_transport_is_detected_and_fixed_once():
    loop = _StubSubprocessLoop()
    try:
        assert not loop_supports_subprocesses(loop)
        assert ensure_subprocess_support(loop)
        assert loop_supports_subprocesses(loop)
        assert not ensure_subprocess_support(loop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows selector loop only")
def test_windows_selector_loop_has_no_native_subprocess_support():
    loop = asyncio.SelectorEventLoop()
    try:
        assert not loop_supports_subprocesses(loop)
    finally:
        loop.close()


def test_communicate_streams_large_input_and_output(threaded_loop):
    async def run():
        process = await asyncio.create_subprocess_exec(
            *_python(
                "import sys; data = sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data); "
                "sys.stderr.write('done'); sys.exit(3)"
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = bytes(range(256)) * (ONE_MIB // 256)
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), 30)
        return process, payload, stdout, stderr

    process, payload, stdout, stderr = threaded_loop.run_until_complete(run())
    assert stdout == payload
    assert stderr == b"done"
    assert process.returncode == 3


def test_stream_reads_and_writes_interleave_with_drain(threaded_loop):
    async def run():
        process = await asyncio.create_subprocess_exec(
            *_python(
                "import sys\n"
                "for line in sys.stdin:\n"
                "    sys.stdout.write(line.upper()); sys.stdout.flush()\n"
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        replies = []
        for word in ("alpha", "beta"):
            process.stdin.write(f"{word}\n".encode())
            await process.stdin.drain()
            replies.append(await asyncio.wait_for(process.stdout.readline(), 10))
        process.stdin.close()
        await process.stdin.wait_closed()
        return replies, await asyncio.wait_for(process.wait(), 10)

    replies, code = threaded_loop.run_until_complete(run())
    assert [reply.strip() for reply in replies] == [b"ALPHA", b"BETA"]
    assert code == 0


def test_redirected_output_and_shell_commands(threaded_loop, tmp_path: Path):
    log = tmp_path / "out.log"

    async def run():
        with log.open("wb") as handle:
            process = await asyncio.create_subprocess_exec(
                *_python("import sys; print('out'); print('err', file=sys.stderr)"),
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
            )
            exec_code = await asyncio.wait_for(process.wait(), 10)
        shell = await asyncio.create_subprocess_shell(
            "echo shell-ok", stdout=asyncio.subprocess.PIPE
        )
        shell_out, _ = await asyncio.wait_for(shell.communicate(), 10)
        return exec_code, shell_out

    exec_code, shell_out = threaded_loop.run_until_complete(run())
    assert exec_code == 0
    assert sorted(log.read_bytes().split()) == [b"err", b"out"]
    assert shell_out.strip() == b"shell-ok"


def test_timeout_cancellation_and_kill_keep_process_ownership(threaded_loop):
    async def run():
        process = await asyncio.create_subprocess_exec(
            *_python("import time; time.sleep(60)"),
            stdout=asyncio.subprocess.PIPE,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(process.wait(), 0.2)
        waiter = asyncio.ensure_future(process.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert process.returncode is None
        process.kill()
        return await asyncio.wait_for(process.wait(), 10)

    started = time.monotonic()
    code = threaded_loop.run_until_complete(run())
    assert code != 0
    assert time.monotonic() - started < 20


def test_command_runner_timeout_kills_process_tree(threaded_loop, tmp_path: Path):
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )

    async def run():
        return await SubprocessCommandRunner().run(
            _python(script),
            cwd=tmp_path,
            timeout_seconds=1,
            log_path=tmp_path / "runner.log",
            environment={},
            display_name="probe",
        )

    started = time.monotonic()
    result = threaded_loop.run_until_complete(run())
    assert result.timed_out
    assert time.monotonic() - started < 20


def test_failed_spawn_raises_like_native_transport(threaded_loop, tmp_path: Path):
    async def run():
        await asyncio.create_subprocess_exec(
            str(tmp_path / "missing-program"), stdout=subprocess.PIPE
        )

    with pytest.raises(OSError):
        threaded_loop.run_until_complete(run())
