"""Tests for trace context propagation (threading.local + ContextVar)."""

import asyncio
import threading

from incidentary.context import (
    TraceContext,
    clear_trace_context,
    get_trace_context,
    set_trace_context,
)


def test_get_trace_context_returns_none_when_not_set():
    clear_trace_context()
    assert get_trace_context() is None


def test_set_and_get_round_trip():
    clear_trace_context()
    set_trace_context("trace-abc", "ce-123")
    ctx = get_trace_context()
    assert ctx is not None
    assert ctx.trace_id == "trace-abc"
    assert ctx.ce_id == "ce-123"
    clear_trace_context()


def test_clear_removes_context():
    set_trace_context("trace-1", "ce-1")
    assert get_trace_context() is not None
    clear_trace_context()
    assert get_trace_context() is None


def test_trace_context_is_frozen():
    ctx = TraceContext(trace_id="t", ce_id="c")
    assert ctx.trace_id == "t"
    assert ctx.ce_id == "c"
    try:
        ctx.trace_id = "changed"  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except AttributeError:
        pass


def test_threading_isolation():
    """Two threads set different contexts; each reads its own."""
    clear_trace_context()
    results: dict[str, str | None] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, trace_id: str, ce_id: str):
        set_trace_context(trace_id, ce_id)
        barrier.wait(timeout=2)
        ctx = get_trace_context()
        results[name] = ctx.trace_id if ctx else None

    t1 = threading.Thread(target=worker, args=("a", "trace-a", "ce-a"))
    t2 = threading.Thread(target=worker, args=("b", "trace-b", "ce-b"))
    t1.start()
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert results["a"] == "trace-a"
    assert results["b"] == "trace-b"
    clear_trace_context()


def test_async_isolation():
    """Two async tasks set different contexts; each reads its own."""
    clear_trace_context()
    results: dict[str, str | None] = {}
    event = asyncio.Event()

    async def worker(name: str, trace_id: str, ce_id: str):
        set_trace_context(trace_id, ce_id)
        # Wait so both tasks overlap
        if name == "a":
            event.set()
        else:
            await asyncio.sleep(0)  # yield to let "a" set its context
        ctx = get_trace_context()
        results[name] = ctx.trace_id if ctx else None

    async def run():
        await asyncio.gather(
            worker("a", "trace-async-a", "ce-async-a"),
            worker("b", "trace-async-b", "ce-async-b"),
        )

    asyncio.run(run())

    assert results["a"] == "trace-async-a"
    assert results["b"] == "trace-async-b"
    clear_trace_context()
