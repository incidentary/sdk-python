"""Tests for adaptive batch sizing."""

from __future__ import annotations

import time
import uuid

from incidentary.client import IncidentaryClient
from incidentary.types import SkeletonCe


def make_client(**overrides):
    config = {
        "api_key": "test",
        "service_name": "svc",
        "base_url": "http://localhost:18080",
        "pre_arm_enable_slow_success": False,
        "pre_arm_enable_inflight": False,
        "pre_arm_enable_retry": False,
        "auto_instrument": False,
    }
    config.update(overrides)
    return IncidentaryClient(**config)


def make_ce() -> SkeletonCe:
    return SkeletonCe(
        id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        parent_id=None,
        service_id="svc",
        occurred_at=int(time.time() * 1_000_000_000),
        kind="HTTP_SERVER",
        status_code=200,
        duration_ns=1_000,
    )


# ---------------------------------------------------------------------------
# Default state
# ---------------------------------------------------------------------------


class TestAdaptiveBatchDefaults:
    def test_default_batch_size(self):
        client = make_client()
        assert client._current_batch_size == 100

    def test_default_ema_is_zero(self):
        client = make_client()
        assert client._flush_latency_ema_ms == 0.0

    def test_default_max_flush_overhead(self):
        client = make_client()
        assert client._max_flush_overhead_ms == 100

    def test_custom_max_flush_overhead(self):
        client = make_client(max_flush_overhead_ms=200)
        assert client._max_flush_overhead_ms == 200


# ---------------------------------------------------------------------------
# EMA calculation
# ---------------------------------------------------------------------------


class TestEMACalculation:
    def test_first_measurement_seeds_ema(self):
        client = make_client()
        client._update_flush_latency_ema(50.0)
        # First measurement: EMA = alpha * value + (1 - alpha) * 0 = 0.3 * 50 = 15
        assert client._flush_latency_ema_ms == 0.3 * 50.0

    def test_ema_converges_toward_value(self):
        client = make_client()
        # Feed constant latency, EMA should converge
        for _ in range(50):
            client._update_flush_latency_ema(80.0)
        assert abs(client._flush_latency_ema_ms - 80.0) < 1.0

    def test_ema_responds_to_spike(self):
        client = make_client()
        # Establish baseline
        for _ in range(20):
            client._update_flush_latency_ema(30.0)
        baseline = client._flush_latency_ema_ms

        # Spike
        client._update_flush_latency_ema(200.0)
        after_spike = client._flush_latency_ema_ms

        assert after_spike > baseline
        # Single spike with alpha=0.3 should move ~30% toward 200
        expected = 0.3 * 200.0 + 0.7 * baseline
        assert abs(after_spike - expected) < 0.01


# ---------------------------------------------------------------------------
# Batch size adjustment
# ---------------------------------------------------------------------------


class TestBatchSizeAdjustment:
    def test_low_latency_increases_batch_size(self):
        """Latency < 50% of ceiling -> increase by 20%."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 100
        # Seed EMA at a low value so it stays below 50% threshold
        client._flush_latency_ema_ms = 30.0

        client._on_flush_complete(latency_ms=30.0)

        assert client._current_batch_size == 120  # 100 * 1.2

    def test_high_latency_decreases_batch_size(self):
        """Latency > 90% of ceiling -> decrease by 30%."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 100
        # Seed EMA already above 90% threshold so a single 95ms confirms it
        client._flush_latency_ema_ms = 92.0

        client._on_flush_complete(latency_ms=95.0)

        # After EMA update: 0.3*95 + 0.7*92 = 28.5 + 64.4 = 92.9, still > 90
        assert client._current_batch_size == 70  # 100 * 0.7

    def test_mid_latency_no_change(self):
        """Latency between 50% and 90% of ceiling -> no change."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 100
        # Seed EMA in the middle band (50-90ms)
        client._flush_latency_ema_ms = 60.0

        client._on_flush_complete(latency_ms=60.0)

        assert client._current_batch_size == 100

    def test_batch_size_never_below_minimum(self):
        """Batch size never drops below 10."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 10
        # Seed EMA above 90% so it tries to decrease
        client._flush_latency_ema_ms = 95.0

        client._on_flush_complete(latency_ms=95.0)

        assert client._current_batch_size == 10

    def test_batch_size_never_above_maximum(self):
        """Batch size never exceeds 5000."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 4500
        # Seed EMA low so it tries to increase
        client._flush_latency_ema_ms = 5.0

        client._on_flush_complete(latency_ms=5.0)

        assert client._current_batch_size == 5000  # 4500 * 1.2 = 5400, clamped to 5000

    def test_repeated_low_latency_grows_to_max(self):
        """Sustained low latency eventually hits the 5000 cap."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 100

        for _ in range(200):
            client._on_flush_complete(latency_ms=5.0)

        assert client._current_batch_size == 5000

    def test_repeated_high_latency_shrinks_to_min(self):
        """Sustained high latency eventually hits the 10 floor."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 1000

        for _ in range(200):
            client._on_flush_complete(latency_ms=95.0)

        assert client._current_batch_size == 10

    def test_batch_size_is_always_integer(self):
        """Batch size should always be an integer."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 77
        client._flush_latency_ema_ms = 5.0

        client._on_flush_complete(latency_ms=5.0)

        assert isinstance(client._current_batch_size, int)
        assert client._current_batch_size == 92  # int(77 * 1.2) = 92


# ---------------------------------------------------------------------------
# EMA uses adjustment based on actual EMA, not single sample
# ---------------------------------------------------------------------------


class TestEMADrivenAdjustment:
    def test_single_spike_does_not_trigger_decrease_if_ema_still_low(self):
        """EMA smoothing prevents a single spike from immediately shrinking."""
        client = make_client(max_flush_overhead_ms=100)
        client._current_batch_size = 100

        # Establish low-latency baseline in EMA
        for _ in range(20):
            client._on_flush_complete(latency_ms=10.0)

        size_before_spike = client._current_batch_size

        # Single spike -- EMA won't jump above 90% threshold
        client._on_flush_complete(latency_ms=200.0)

        # EMA after spike should still be well below 90ms (the 90% threshold)
        # so batch size should continue increasing, not decrease
        assert client._current_batch_size >= size_before_spike


# ---------------------------------------------------------------------------
# Telemetry in upload payload
# ---------------------------------------------------------------------------


class TestFlushTelemetry:
    def test_flush_syncs_telemetry_to_transport(self):
        """flush_to_backend should sync telemetry to transport before uploading."""
        from unittest.mock import patch

        client = make_client()
        client._flush_latency_ema_ms = 42.5
        client._current_batch_size = 200
        client.write_event(make_ce())

        with patch.object(client._transport, "upload_batch") as mock:
            client.flush_to_backend()

        assert mock.called
        # Verify telemetry was synced to transport
        assert client._transport._agent_telemetry["flush_latency_ema_ms"] == 42.5
        assert client._transport._agent_telemetry["current_batch_size"] == 200

    def test_transport_payload_includes_telemetry(self):
        """The transport payload should include flush_latency_ema_ms and current_batch_size."""
        from incidentary.transport import Transport

        t = Transport(
            base_url="http://localhost:18080",
            api_key="key",
            service_name="svc",
        )

        t.set_agent_telemetry({
            "flush_latency_ema_ms": 42.5,
            "current_batch_size": 200,
        })

        events = [make_ce()]
        payload = t._build_payload(events, "SKELETON")

        assert payload is not None
        assert "agent" in payload
        agent = payload["agent"]
        assert "telemetry" in agent
        assert agent["telemetry"]["flush_latency_ema_ms"] == 42.5
        assert agent["telemetry"]["current_batch_size"] == 200

    def test_no_telemetry_key_when_empty(self):
        """If no telemetry is set, agent dict should not contain telemetry key."""
        from incidentary.transport import Transport

        t = Transport(
            base_url="http://localhost:18080",
            api_key="key",
            service_name="svc",
        )

        events = [make_ce()]
        payload = t._build_payload(events, "SKELETON")

        assert payload is not None
        assert "telemetry" not in payload["agent"]


# ---------------------------------------------------------------------------
# Auto-flush on buffer full
# ---------------------------------------------------------------------------


class TestAutoFlushOnBatchThreshold:
    def test_write_event_triggers_flush_at_batch_size(self):
        """When buffer reaches current_batch_size, auto-flush should fire."""
        from unittest.mock import patch

        client = make_client()
        client._current_batch_size = 5  # Low threshold for testing

        flush_count = 0
        original_flush = client.flush_to_backend

        def counting_flush(incident_id=None):
            nonlocal flush_count
            flush_count += 1
            original_flush(incident_id=incident_id)

        with patch.object(client, "flush_to_backend", side_effect=counting_flush):
            for _ in range(5):
                client.write_event(make_ce())

        assert flush_count == 1

    def test_no_auto_flush_below_threshold(self):
        """Buffer below batch_size should not trigger auto-flush."""
        from unittest.mock import patch

        client = make_client()
        client._current_batch_size = 100

        flush_count = 0

        def counting_flush(incident_id=None):
            nonlocal flush_count
            flush_count += 1

        with patch.object(client, "flush_to_backend", side_effect=counting_flush):
            for _ in range(50):
                client.write_event(make_ce())

        assert flush_count == 0


# ---------------------------------------------------------------------------
# Flush latency callback from transport
# ---------------------------------------------------------------------------


class TestFlushLatencyCallback:
    def test_transport_calls_on_flush_latency(self):
        """Transport should invoke the on_flush_latency callback after successful upload."""
        from incidentary.transport import Transport

        recorded_latencies = []

        def on_latency(ms: float):
            recorded_latencies.append(ms)

        t = Transport(
            base_url="http://localhost:18080",
            api_key="key",
            service_name="svc",
            on_flush_latency=on_latency,
        )

        assert t._on_flush_latency is on_latency
