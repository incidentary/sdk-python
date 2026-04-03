"""Trace context propagation for WSGI (threading.local) and ASGI (ContextVar)."""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    ce_id: str


_sync_context = threading.local()
_async_context: ContextVar[TraceContext | None] = ContextVar("incidentary_trace", default=None)


def set_trace_context(trace_id: str, ce_id: str) -> None:
    """Set trace context in both sync and async stores."""
    ctx = TraceContext(trace_id=trace_id, ce_id=ce_id)
    _sync_context.trace = ctx
    _async_context.set(ctx)


def get_trace_context() -> TraceContext | None:
    """Get current trace context. Tries ContextVar first (async), falls back to threading.local (sync)."""
    ctx = _async_context.get(None)
    if ctx is not None:
        return ctx
    return getattr(_sync_context, "trace", None)


def clear_trace_context() -> None:
    """Clear trace context from both stores."""
    _sync_context.trace = None
    _async_context.set(None)
