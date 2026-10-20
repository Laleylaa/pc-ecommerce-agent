"""Real server loop and independent clock origins for deadline regressions."""

import asyncio

import pytest

LOOP_KINDS = ("asyncio", "uvloop", "ahead", "behind")


def new_loop(kind):
    if kind == "uvloop":
        return pytest.importorskip("uvloop").new_event_loop()
    if kind == "asyncio":
        return asyncio.new_event_loop()

    class OffsetLoop(asyncio.SelectorEventLoop):
        def time(self):
            return super().time() + (10_000_000 if kind == "ahead" else -10_000_000)

    return OffsetLoop()
