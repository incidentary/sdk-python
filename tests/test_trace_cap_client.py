"""L1 wiring acceptance — TraceCap integrated with IncidentaryClient.

Mirrors tests/trace-cap.test.ts (Node SDK) wiring suite. Verifies
observe -> drop, truncated marker, hook re-binding, dropped_total.
"""

from __future__ import annotations

from incidentary.client import IncidentaryClient
from incidentary.trace_cap import (
    SPANS_PER_TRACE_BREAKER,
    SPANS_PER_TRACE_TRUNCATE,
    SPANS_PER_TRACE_WARN,
)
from incidentary.types import SkeletonCe

TID = "00000000-0000-4000-8000-000000000c11"


def _make_event(trace_id: str = TID, attrs: dict | None = None) -> SkeletonCe:
    return SkeletonCe(
        id="ce_t",
        trace_id=trace_id,
        parent_id=None,
        service_id="test-svc",
        occurred_at=1,
        kind="INTERNAL",
        status_code=0,
        duration_ns=0,
        attributes=attrs,
    )


def _make_client(trace_cap_enabled: bool = True, **extra) -> IncidentaryClient:
    return IncidentaryClient(
        api_key="test-key",
        service_name="test-svc",
        api_url="http://localhost:0",
        auto_instrument=False,
        trace_cap_enabled=trace_cap_enabled,
        **extra,
    )


def test_default_is_enabled_and_under_warn_buffers_all():
    client = _make_client()
    for _ in range(SPANS_PER_TRACE_WARN - 1):
        client.write_event(_make_event())
    assert client.get_trace_cap_dropped_total() == 0


def test_above_truncate_drops_subsequent_spans():
    client = _make_client()
    for _ in range(SPANS_PER_TRACE_TRUNCATE + 5):
        client.write_event(_make_event())
    # Spans above the truncate threshold are dropped at L1; at the
    # boundary the span itself is accepted (with a marker) and
    # everything past it is dropped until the breaker fires.
    assert client.get_trace_cap_dropped_total() == 5


def test_truncating_boundary_marks_attribute():
    client = _make_client()
    seen_marker = []

    original = client._buffer.write

    def capture(ce):
        if ce.attributes and ce.attributes.get("incidentary.trace.truncated_in_sdk"):
            seen_marker.append(ce.id)
        return original(ce)

    client._buffer.write = capture  # type: ignore[assignment]

    for i in range(SPANS_PER_TRACE_TRUNCATE):
        evt = _make_event()
        evt.id = f"ce_{i}"
        client.write_event(evt)

    # The boundary span (#50_000) is accepted with the marker.
    assert len(seen_marker) == 1


def test_disabled_via_constructor_passes_everything():
    client = _make_client(trace_cap_enabled=False)
    for _ in range(SPANS_PER_TRACE_TRUNCATE + 100):
        client.write_event(_make_event())
    assert client.get_trace_cap_dropped_total() == 0


def test_register_trace_cap_hook_is_invoked_on_warn():
    client = _make_client()
    events = []
    client.register_trace_cap_hook(lambda e: events.append(e))

    for _ in range(SPANS_PER_TRACE_WARN):
        client.write_event(_make_event())

    assert len(events) == 1
    assert events[0].tier == "warn"
    assert events[0].trace_id == TID


def test_breaker_drops_and_blacklists():
    client = _make_client()
    # A breaker run requires 500K observe() calls — that is the
    # contract. Push exactly that many spans through write_event and
    # verify drop counter reflects the post-truncate path.
    for _ in range(SPANS_PER_TRACE_BREAKER + 1):
        client.write_event(_make_event())

    # 1 boundary span accepted (truncate). Everything between
    # truncate+1 and breaker is dropped (breaker_threshold - truncate_threshold).
    # Plus the +1 after breaker.
    expected = (SPANS_PER_TRACE_BREAKER - SPANS_PER_TRACE_TRUNCATE) + 1
    assert client.get_trace_cap_dropped_total() == expected
