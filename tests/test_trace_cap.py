"""L1 — SDK-side trace cap (cross-SDK spec at docs/specs/l1-trace-cap.md).

Mirror of the Node SDK acceptance suite. Same test names, same semantics.
"""

import pytest

from incidentary.trace_cap import (
    SPANS_PER_TRACE_BREAKER,
    SPANS_PER_TRACE_TRUNCATE,
    SPANS_PER_TRACE_WARN,
    TraceCap,
    TraceCapEvent,
)

TID_A = "00000000-0000-4000-8000-0000000000a1"
TID_B = "00000000-0000-4000-8000-0000000000b2"


def make_cap(**opts):
    events: list[TraceCapEvent] = []
    cap = TraceCap(
        service_id=opts.pop("service_id", "test-svc"),
        hook=lambda e: events.append(e),
        **opts,
    )
    return cap, events


def emit_n(cap: TraceCap, trace_id: str, n: int):
    accepted = 0
    dropped = 0
    for _ in range(n):
        verdict = cap.observe(trace_id)
        if verdict.should_drop:
            dropped += 1
        else:
            accepted += 1
    return accepted, dropped


# ---------------------------------------------------------------------------
# Exported constants


def test_constants_match_spec():
    assert SPANS_PER_TRACE_WARN == 5_000
    assert SPANS_PER_TRACE_TRUNCATE == 50_000
    assert SPANS_PER_TRACE_BREAKER == 500_000


# ---------------------------------------------------------------------------
# Acceptance tests (spec parity)


def test_under_warn_threshold_passes_all_spans():
    cap, events = make_cap()
    accepted, dropped = emit_n(cap, TID_A, SPANS_PER_TRACE_WARN - 1)
    assert accepted == SPANS_PER_TRACE_WARN - 1
    assert dropped == 0
    assert events == []


def test_at_warn_threshold_fires_once():
    cap, events = make_cap()
    accepted, dropped = emit_n(cap, TID_A, SPANS_PER_TRACE_WARN)
    assert accepted == SPANS_PER_TRACE_WARN
    assert dropped == 0
    assert len(events) == 1
    ev = events[0]
    assert ev.tier == "warn"
    assert ev.trace_id == TID_A
    assert ev.cumulative_span_count == SPANS_PER_TRACE_WARN
    assert ev.service_id == "test-svc"
    assert isinstance(ev.timestamp_ms, int)


def test_crossing_warn_in_one_span_fires_once_only():
    cap, events = make_cap()
    emit_n(cap, TID_A, SPANS_PER_TRACE_WARN)
    emit_n(cap, TID_A, 1_000)
    warn_events = [e for e in events if e.tier == "warn"]
    assert len(warn_events) == 1


def test_at_truncate_threshold_drops_subsequent():
    cap, events = make_cap()
    a1, d1 = emit_n(cap, TID_A, SPANS_PER_TRACE_TRUNCATE)
    assert a1 == SPANS_PER_TRACE_TRUNCATE
    assert d1 == 0
    a2, d2 = emit_n(cap, TID_A, 1_000)
    assert a2 == 0
    assert d2 == 1_000
    truncates = [e for e in events if e.tier == "truncate"]
    assert len(truncates) == 1
    assert truncates[0].cumulative_span_count == SPANS_PER_TRACE_TRUNCATE


def test_at_breaker_threshold_drops_subsequent():
    cap, events = make_cap()
    emit_n(cap, TID_A, SPANS_PER_TRACE_BREAKER)
    tiers = sorted({e.tier for e in events})
    assert tiers == ["breaker", "truncate", "warn"]
    next_a, next_d = emit_n(cap, TID_A, 1)
    assert next_d == 1
    assert len(events) == 3


def test_distinct_trace_ids_isolated():
    cap, events = make_cap()
    emit_n(cap, TID_A, SPANS_PER_TRACE_WARN - 1)
    emit_n(cap, TID_B, SPANS_PER_TRACE_WARN - 1)
    assert events == []


def test_lru_evicts_oldest_under_pressure():
    cap, events = make_cap(max_tracked_traces=8)
    cap.observe(TID_A)
    for i in range(16):
        cap.observe(f"evict-{i}-{TID_B}")
    emit_n(cap, TID_A, SPANS_PER_TRACE_WARN)
    warns = [e for e in events if e.tier == "warn"]
    assert len(warns) == 1


def test_breaker_blacklist_persists_across_evictions():
    cap, _events = make_cap(max_tracked_traces=8)
    emit_n(cap, TID_A, SPANS_PER_TRACE_BREAKER)
    for i in range(16):
        cap.observe(f"flood-{i}")
    verdict = cap.observe(TID_A)
    assert verdict.should_drop
    assert verdict.reason == "breaker"


def test_opt_out_disables_all_caps():
    cap, events = make_cap(enabled=False)
    accepted, dropped = emit_n(cap, TID_A, 600_000)
    assert accepted == 600_000
    assert dropped == 0
    assert events == []


def test_hook_receives_correct_payload():
    received = []
    cap = TraceCap(service_id="svc-payments", hook=received.append)
    emit_n(cap, TID_A, SPANS_PER_TRACE_WARN)
    assert len(received) == 1
    ev = received[0]
    assert ev.tier == "warn"
    assert ev.trace_id == TID_A
    assert ev.cumulative_span_count == SPANS_PER_TRACE_WARN
    assert ev.service_id == "svc-payments"


# ---------------------------------------------------------------------------
# Defensive paths


def test_empty_or_none_trace_id_accepted():
    cap, _ = make_cap()
    assert not cap.observe("").should_drop
    assert not cap.observe(None).should_drop  # type: ignore[arg-type]


def test_hook_errors_swallowed():
    def bad_hook(_e):
        raise RuntimeError("boom")

    cap = TraceCap(service_id="svc", hook=bad_hook)
    # Crossing warn must not raise.
    emit_n(cap, TID_A, SPANS_PER_TRACE_WARN)


def test_blacklist_itself_is_bounded():
    cap, _ = make_cap(max_blacklisted_traces=4)
    for i in range(6):
        emit_n(cap, f"breaker-{i}", SPANS_PER_TRACE_BREAKER)
    verdict = cap.observe("breaker-0")
    assert not verdict.should_drop
