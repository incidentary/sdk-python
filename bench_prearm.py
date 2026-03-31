"""Synthetic pre-arm trigger benchmarks for sdk-python."""

from __future__ import annotations

import time

from incidentary.prearm_triggers import (
    InFlightConfig,
    RequestSignal,
    RetryConfig,
    SlowSuccessConfig,
    TriggerEngine,
    TriggerEngineConfig,
)
from incidentary.types import CaptureMode


BASE_CONFIG = TriggerEngineConfig(
    enable_slow_success=True,
    enable_in_flight_pileup=True,
    enable_retry_onset=True,
    slow_success=SlowSuccessConfig(
        min_slow_duration_ns=250_000_000,
        slow_multiplier=2.0,
        ewma_alpha=0.1,
        high_rate=0.2,
        mild_rate=0.1,
        min_samples=50,
        include_4xx_as_success_like=True,
        min_baseline_ns=1_000_000,
        max_baseline_ns=60_000_000_000,
    ),
    in_flight=InFlightConfig(
        min_absolute_in_flight=32,
        baseline_multiplier=2.0,
        net_growth_min=16,
        severe_hold_secs=3,
        mild_hold_secs=2,
        baseline_alpha=0.05,
    ),
    retry=RetryConfig(
        retry_window_ms=5_000,
        high_rate=0.1,
        mild_rate=0.05,
        min_total_attempts=20,
        table_size=4_096,
    ),
)


def signal_in() -> RequestSignal:
    return RequestSignal(
        kind="HTTP_IN",
        status_code=200,
        duration_ns=120_000_000,
        cancelled=False,
        timed_out=False,
        outbound_retry_key_hash=0,
        outbound_retry_key_quality="unknown",
        explicit_retry_observed=None,
    )


def signal_out(hash_value: int) -> RequestSignal:
    return RequestSignal(
        kind="HTTP_OUT",
        status_code=200,
        duration_ns=90_000_000,
        cancelled=False,
        timed_out=False,
        outbound_retry_key_hash=hash_value,
        outbound_retry_key_quality="route_template",
        explicit_retry_observed=None,
    )


def bench_request_path(events: int) -> tuple[float, float]:
    engine = TriggerEngine(BASE_CONFIG)
    start = time.perf_counter()
    for i in range(events):
        sec = i // 1_000
        mono = i
        engine.on_request_start(sec)
        engine.on_request_complete(signal_in(), sec, mono)
    elapsed = time.perf_counter() - start
    ops_per_sec = events / elapsed if elapsed > 0 else 0.0
    us_per_event = elapsed * 1_000_000 / events if events > 0 else 0.0
    return ops_per_sec, us_per_event


def bench_retry_table(events: int, collision: bool) -> tuple[float, float]:
    engine = TriggerEngine(BASE_CONFIG)
    start = time.perf_counter()
    for i in range(events):
        sec = i // 1_000
        mono = i
        h = 42 if collision else (i * 2654435761) & 0xFFFFFFFF
        engine.on_request_complete(signal_out(h), sec, mono)
    elapsed = time.perf_counter() - start
    ops_per_sec = events / elapsed if elapsed > 0 else 0.0
    us_per_event = elapsed * 1_000_000 / events if events > 0 else 0.0
    return ops_per_sec, us_per_event


def bench_bucket_rotation(iterations: int) -> tuple[float, float]:
    engine = TriggerEngine(BASE_CONFIG)
    start = time.perf_counter()
    sec = 0
    for i in range(iterations):
        sec += 1
        engine.on_request_start(sec)
        engine.on_request_complete(signal_in(), sec, i)
    elapsed = time.perf_counter() - start
    ops_per_sec = iterations / elapsed if elapsed > 0 else 0.0
    us_per_rotation = elapsed * 1_000_000 / iterations if iterations > 0 else 0.0
    return ops_per_sec, us_per_rotation


def bench_evaluate(iterations: int) -> tuple[float, float]:
    engine = TriggerEngine(BASE_CONFIG)
    for i in range(10_000):
        sec = i // 1000
        engine.on_request_start(sec)
        engine.on_request_complete(signal_in(), sec, i)

    start = time.perf_counter()
    for i in range(iterations):
        sec = i // 1000
        engine.evaluate(CaptureMode.NORMAL, i, sec, i)
    elapsed = time.perf_counter() - start
    ops_per_sec = iterations / elapsed if elapsed > 0 else 0.0
    us_per_eval = elapsed * 1_000_000 / iterations if iterations > 0 else 0.0
    return ops_per_sec, us_per_eval


def main() -> None:
    print("sdk-python pre-arm trigger benchmarks")

    for events in (100_000, 300_000, 500_000):
        ops, latency = bench_request_path(events)
        print(f"request_path events={events}: {ops:,.0f} ops/s ({latency:.2f} us/event)")

    ops, latency = bench_retry_table(200_000, collision=False)
    print(f"retry_table moderate_collision: {ops:,.0f} ops/s ({latency:.2f} us/update)")

    ops, latency = bench_retry_table(200_000, collision=True)
    print(f"retry_table high_collision: {ops:,.0f} ops/s ({latency:.2f} us/update)")

    ops, latency = bench_bucket_rotation(100_000)
    print(f"bucket_rotation: {ops:,.0f} ops/s ({latency:.2f} us/rotation)")

    ops, latency = bench_evaluate(200_000)
    print(f"evaluate: {ops:,.0f} ops/s ({latency:.2f} us/evaluate)")


if __name__ == "__main__":
    main()
