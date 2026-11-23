"""Subprocess capability detection follows the loop, not the platform."""

import asyncio
import sys

import pytest

from kerkov.core.process.subprocess_support import loop_supports_subprocesses


class _StubSubprocessLoop(asyncio.SelectorEventLoop):
    _make_subprocess_transport = asyncio.BaseEventLoop._make_subprocess_transport


def test_default_loop_supports_subprocesses():
    loop = asyncio.new_event_loop()
    try:
        assert loop_supports_subprocesses(loop)
    finally:
        loop.close()


def test_loop_with_unimplemented_transport_is_detected():
    loop = _StubSubprocessLoop()
    try:
        assert not loop_supports_subprocesses(loop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows selector loop only")
def test_windows_selector_loop_has_no_subprocess_support():
    loop = asyncio.SelectorEventLoop()
    try:
        assert not loop_supports_subprocesses(loop)
    finally:
        loop.close()
