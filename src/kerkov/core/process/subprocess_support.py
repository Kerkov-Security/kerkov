"""Detect whether an asyncio event loop can start subprocesses."""

from __future__ import annotations

import asyncio


def loop_supports_subprocesses(loop: asyncio.AbstractEventLoop) -> bool:
    """Return False when ``loop`` keeps asyncio's unimplemented subprocess stub.

    Windows selector loops, such as the one Daphne's Twisted reactor runs on, have
    no subprocess transport, so ``asyncio.create_subprocess_exec`` raises
    ``NotImplementedError`` there.
    """
    if not isinstance(loop, asyncio.BaseEventLoop):
        return True
    make_transport = getattr(loop._make_subprocess_transport, "__func__", None)
    return make_transport is not asyncio.BaseEventLoop._make_subprocess_transport
