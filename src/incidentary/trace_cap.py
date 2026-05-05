"""L1 — SDK-side per-trace span cap.

Cross-SDK spec: docs/specs/l1-trace-cap.md (in the main incidentary repo).
Threshold parity is mandatory:
  - apps/api/src/billing/trace_meter.rs (Rust API)
  - processor/incidentaryprocessor/trace_breaker.go (Bridge)
  - SDKs: Node, Go, .NET share these same constants

Catches single-process runaway traces at the source. Memory bounded
by an LRU on counters (default 1024) and breaker blacklist (256).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Literal, Optional

# Threshold constants — DO NOT change in isolation. Must stay in sync
# with apps/api/src/billing/trace_meter.rs::SPANS_PER_TRACE_*
# and processor/incidentaryprocessor/trace_breaker.go.
SPANS_PER_TRACE_WARN: int = 5_000
SPANS_PER_TRACE_TRUNCATE: int = 50_000
SPANS_PER_TRACE_BREAKER: int = 500_000

DEFAULT_MAX_TRACKED_TRACES: int = 1_024
DEFAULT_MAX_BLACKLISTED_TRACES: int = 256

TraceCapTier = Literal["warn", "truncate", "breaker"]


@dataclass
class TraceCapEvent:
    """Structured payload emitted on every tier transition."""

    tier: TraceCapTier
    trace_id: str
    cumulative_span_count: int
    service_id: str
    timestamp_ms: int


@dataclass
class Verdict:
    """Result of TraceCap.observe(); shape mirrors the Node SDK."""

    should_drop: bool
    tier: Literal["none", "warn", "truncating"] = "none"
    reason: Optional[Literal["truncate", "breaker"]] = None


_ACCEPT_NONE = Verdict(should_drop=False, tier="none")
_ACCEPT_WARN = Verdict(should_drop=False, tier="warn")
_ACCEPT_TRUNCATING = Verdict(should_drop=False, tier="truncating")
_DROP_TRUNCATE = Verdict(should_drop=True, reason="truncate")
_DROP_BREAKER = Verdict(should_drop=True, reason="breaker")


class _BoundedLRU:
    """OrderedDict-backed LRU with O(1) eviction."""

    __slots__ = ("_store", "_max")

    def __init__(self, max_size: int) -> None:
        self._store: OrderedDict = OrderedDict()
        self._max = max_size

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        return key in self._store

    def get(self, key, default=None):
        if key in self._store:
            self._store.move_to_end(key)
            return self._store[key]
        return default

    def set(self, key, value) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self._max:
            self._store.popitem(last=False)

    def delete(self, key) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False


def _default_hook(event: TraceCapEvent) -> None:
    import json
    import sys

    msg = json.dumps(
        {
            "event": "incidentary_trace_cap_tier",
            "tier": event.tier,
            "trace_id": event.trace_id,
            "cumulative_span_count": event.cumulative_span_count,
            "service_id": event.service_id,
            "timestamp_ms": event.timestamp_ms,
        }
    )
    print(msg, file=sys.stderr)


class TraceCap:
    """Per-SDK-instance, per-trace span cap.

    See docs/specs/l1-trace-cap.md for the full contract.
    """

    __slots__ = (
        "_service_id",
        "_hook",
        "_enabled",
        "_counters",
        "_blacklist",
        "_transitions_emitted",
    )

    def __init__(
        self,
        *,
        service_id: str,
        hook: Optional[Callable[[TraceCapEvent], None]] = None,
        enabled: bool = True,
        max_tracked_traces: int = DEFAULT_MAX_TRACKED_TRACES,
        max_blacklisted_traces: int = DEFAULT_MAX_BLACKLISTED_TRACES,
    ) -> None:
        self._service_id = service_id
        self._hook = hook or _default_hook
        self._enabled = enabled
        self._counters = _BoundedLRU(max_tracked_traces)
        self._blacklist = _BoundedLRU(max_blacklisted_traces)
        self._transitions_emitted = _BoundedLRU(max_tracked_traces * 3)

    def observe(self, trace_id: Optional[str]) -> Verdict:
        """Apply the cap to a single span attempt.

        Returns a Verdict describing whether the caller should drop the
        span. Side effects: increments the per-trace counter, may emit
        the tier-transition hook (at most once per trace per tier).
        """
        if not self._enabled or not trace_id:
            return _ACCEPT_NONE

        if trace_id in self._blacklist:
            return _DROP_BREAKER

        prior = self._counters.get(trace_id) or 0
        nxt = prior + 1
        self._counters.set(trace_id, nxt)

        if nxt >= SPANS_PER_TRACE_BREAKER:
            if nxt == SPANS_PER_TRACE_BREAKER:
                self._blacklist.set(trace_id, True)
                self._counters.delete(trace_id)
                self._emit_once(trace_id, "breaker", nxt)
            return _DROP_BREAKER
        if nxt > SPANS_PER_TRACE_TRUNCATE:
            return _DROP_TRUNCATE
        if nxt == SPANS_PER_TRACE_TRUNCATE:
            self._emit_once(trace_id, "truncate", nxt)
            return _ACCEPT_TRUNCATING
        if nxt == SPANS_PER_TRACE_WARN:
            self._emit_once(trace_id, "warn", nxt)
            return _ACCEPT_WARN
        return _ACCEPT_NONE

    def set_hook(self, hook: Callable[[TraceCapEvent], None]) -> None:
        """Replace the tier-transition hook. Safe to call after observe()
        has begun.
        """
        self._hook = hook

    def tracked_trace_count(self) -> int:
        return len(self._counters)

    def blacklisted_trace_count(self) -> int:
        return len(self._blacklist)

    def _emit_once(self, trace_id: str, tier: TraceCapTier, count: int) -> None:
        key = (trace_id, tier)
        if key in self._transitions_emitted:
            return
        self._transitions_emitted.set(key, True)
        try:
            self._hook(
                TraceCapEvent(
                    tier=tier,
                    trace_id=trace_id,
                    cumulative_span_count=count,
                    service_id=self._service_id,
                    timestamp_ms=int(time.time() * 1000),
                )
            )
        except Exception:
            # Hook is customer-controllable; never propagate.
            pass


__all__ = [
    "SPANS_PER_TRACE_BREAKER",
    "SPANS_PER_TRACE_TRUNCATE",
    "SPANS_PER_TRACE_WARN",
    "TraceCap",
    "TraceCapEvent",
    "TraceCapTier",
    "Verdict",
]
