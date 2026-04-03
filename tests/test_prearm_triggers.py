from dataclasses import replace

from incidentary.prearm_triggers import (
    InFlightConfig,
    RequestSignal,
    RetryConfig,
    SlowSuccessConfig,
    TriggerEngine,
    TriggerEngineConfig,
)
from incidentary.types import CaptureMode


def make_config(**overrides) -> TriggerEngineConfig:
    config = TriggerEngineConfig(
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

    return replace(config, **overrides)


def complete(**overrides) -> RequestSignal:
    base = RequestSignal(
        kind="HTTP_IN",
        status_code=200,
        duration_ns=100_000_000,
        cancelled=False,
        timed_out=False,
        outbound_retry_key_hash=0,
        outbound_retry_key_quality="unknown",
        explicit_retry_observed=None,
    )
    data = base.__dict__ | overrides
    return RequestSignal(**data)


def test_slow_success_normal_latency_no_fire():
    engine = TriggerEngine(
        make_config(
            enable_in_flight_pileup=False,
            enable_retry_onset=False,
            slow_success=SlowSuccessConfig(
                min_slow_duration_ns=250_000_000,
                slow_multiplier=1.1,
                ewma_alpha=0.1,
                high_rate=0.15,
                mild_rate=0.1,
                min_samples=50,
                include_4xx_as_success_like=True,
                min_baseline_ns=1_000_000,
                max_baseline_ns=60_000_000_000,
            ),
        )
    )

    for sec in range(12):
        for _ in range(10):
            engine.on_request_complete(complete(duration_ns=100_000_000), sec, sec * 1000)
        assert engine.evaluate(CaptureMode.NORMAL, sec * 1000, sec, sec * 1000) is None

    snapshot = engine.snapshot(12, 12_000)
    assert snapshot.slow_success["severity"] is None


def test_slow_success_spike_transitions_to_severe():
    engine = TriggerEngine(
        make_config(
            enable_in_flight_pileup=False,
            enable_retry_onset=False,
            slow_success=SlowSuccessConfig(
                min_slow_duration_ns=250_000_000,
                slow_multiplier=1.1,
                ewma_alpha=0.1,
                high_rate=0.2,
                mild_rate=0.1,
                min_samples=50,
                include_4xx_as_success_like=True,
                min_baseline_ns=1_000_000,
                max_baseline_ns=60_000_000_000,
            ),
        )
    )

    for sec in range(10):
        for _ in range(10):
            engine.on_request_complete(complete(duration_ns=100_000_000), sec, sec * 1000)
        engine.evaluate(CaptureMode.NORMAL, sec * 1000, sec, sec * 1000)

    sec10 = 10
    for _ in range(10):
        engine.on_request_complete(complete(duration_ns=2_000_000_000), sec10, 10_000)
    engine.evaluate(CaptureMode.NORMAL, 10_000, sec10, 10_000)

    sec11 = 11
    for _ in range(10):
        engine.on_request_complete(complete(duration_ns=2_000_000_000), sec11, 11_000)
    decision = engine.evaluate(CaptureMode.NORMAL, 11_000, sec11, 11_000)

    assert decision is not None
    assert decision.should_enter_prearm is True
    assert any(
        reason.trigger_type == "slow_success" and reason.severity == "severe"
        for reason in decision.reasons
    )


def test_slow_success_low_sample_does_not_fire():
    engine = TriggerEngine(
        make_config(
            enable_in_flight_pileup=False,
            enable_retry_onset=False,
        )
    )

    sec = 1
    for _ in range(20):
        engine.on_request_complete(complete(duration_ns=2_000_000_000), sec, 1_000)

    assert engine.evaluate(CaptureMode.NORMAL, 1_000, sec, 1_000) is None


def test_inflight_balanced_no_fire():
    engine = TriggerEngine(
        make_config(
            enable_slow_success=False,
            enable_retry_onset=False,
        )
    )

    for sec in range(12):
        for _ in range(20):
            engine.on_request_start(sec)
            engine.on_request_complete(complete(), sec, sec * 1000)
        assert engine.evaluate(CaptureMode.NORMAL, sec * 1000, sec, sec * 1000) is None


def test_inflight_growth_fires_severe():
    engine = TriggerEngine(
        make_config(
            enable_slow_success=False,
            enable_retry_onset=False,
        )
    )

    for sec in range(3):
        for _ in range(10):
            engine.on_request_start(sec)
            engine.on_request_complete(complete(), sec, sec * 1000)
        engine.evaluate(CaptureMode.NORMAL, sec * 1000, sec, sec * 1000)

    severe_seen = False
    for sec in range(3, 10):
        for _ in range(20):
            engine.on_request_start(sec)
        for _ in range(2):
            engine.on_request_complete(complete(), sec, sec * 1000)

        decision = engine.evaluate(CaptureMode.NORMAL, sec * 1000, sec, sec * 1000)
        if decision is not None and any(
            reason.trigger_type == "in_flight_pileup" for reason in decision.reasons
        ):
            severe_seen = True

    assert severe_seen is True


def test_inflight_counter_never_negative():
    engine = TriggerEngine(
        make_config(
            enable_slow_success=False,
            enable_retry_onset=False,
        )
    )

    sec = 1
    for _ in range(100):
        engine.on_request_complete(complete(), sec, 1_000)

    snapshot = engine.snapshot(sec, 1_000)
    assert snapshot.in_flight_pileup["current_in_flight"] == 0


def test_retry_explicit_path_fires():
    engine = TriggerEngine(
        make_config(
            enable_slow_success=False,
            enable_in_flight_pileup=False,
        )
    )

    sec = 1
    for i in range(25):
        engine.on_request_complete(
            complete(
                kind="HTTP_OUT",
                explicit_retry_observed=(i % 2 == 0),
                outbound_retry_key_quality="explicit",
            ),
            sec,
            1_000 + i,
        )

    decision = engine.evaluate(CaptureMode.NORMAL, 1_000, sec, 1_000)
    assert decision is not None
    assert any(reason.trigger_type == "retry_onset" for reason in decision.reasons)


def test_retry_heuristic_counts_repeated_attempts():
    engine = TriggerEngine(
        make_config(
            enable_slow_success=False,
            enable_in_flight_pileup=False,
            retry=RetryConfig(
                retry_window_ms=5_000,
                high_rate=0.1,
                mild_rate=0.05,
                min_total_attempts=20,
                table_size=512,
            ),
        )
    )

    sec = 1
    for i in range(30):
        engine.on_request_complete(
            complete(
                kind="HTTP_OUT",
                explicit_retry_observed=None,
                outbound_retry_key_hash=12345,
                outbound_retry_key_quality="route_template",
            ),
            sec,
            1_000 + i,
        )

    decision = engine.evaluate(CaptureMode.NORMAL, 1_000, sec, 1_000)
    assert decision is not None
    assert any(reason.trigger_type == "retry_onset" for reason in decision.reasons)


def test_retry_stale_slots_reset_outside_window():
    engine = TriggerEngine(
        make_config(
            enable_slow_success=False,
            enable_in_flight_pileup=False,
            retry=RetryConfig(
                retry_window_ms=50,
                high_rate=0.1,
                mild_rate=0.05,
                min_total_attempts=2,
                table_size=64,
            ),
        )
    )

    engine.on_request_complete(
        complete(
            kind="HTTP_OUT", outbound_retry_key_hash=7, outbound_retry_key_quality="normalized_url"
        ),
        1,
        1_000,
    )
    engine.on_request_complete(
        complete(
            kind="HTTP_OUT", outbound_retry_key_hash=7, outbound_retry_key_quality="normalized_url"
        ),
        1,
        1_010,
    )

    first = engine.evaluate(CaptureMode.NORMAL, 1_000, 1, 1_010)
    assert first is not None

    engine.on_request_complete(
        complete(
            kind="HTTP_OUT", outbound_retry_key_hash=7, outbound_retry_key_quality="normalized_url"
        ),
        2,
        2_000,
    )
    second = engine.evaluate(CaptureMode.NORMAL, 2_000, 2, 2_000)
    assert second is None


def test_arbiter_two_distinct_mild_triggers_prearm():
    config = make_config(
        slow_success=SlowSuccessConfig(
            min_slow_duration_ns=250_000_000,
            slow_multiplier=1.1,
            ewma_alpha=0.1,
            high_rate=0.9,
            mild_rate=0.1,
            min_samples=10,
            include_4xx_as_success_like=True,
            min_baseline_ns=1_000_000,
            max_baseline_ns=60_000_000_000,
        ),
        retry=RetryConfig(
            retry_window_ms=5_000,
            high_rate=0.9,
            mild_rate=0.05,
            min_total_attempts=10,
            table_size=256,
        ),
        enable_in_flight_pileup=False,
    )
    engine = TriggerEngine(config)

    sec = 1
    for _ in range(20):
        engine.on_request_complete(complete(duration_ns=200_000_000), sec, 1_000)
    for _ in range(3):
        engine.on_request_complete(complete(duration_ns=2_000_000_000), sec, 1_050)
    engine.evaluate(CaptureMode.NORMAL, 1_000, sec, 1_000)

    for i in range(20):
        engine.on_request_complete(
            complete(
                kind="HTTP_OUT",
                explicit_retry_observed=(i % 10 == 0),
                outbound_retry_key_quality="explicit",
            ),
            sec,
            1_100 + i,
        )

    decision = engine.evaluate(CaptureMode.NORMAL, 1_100, sec, 1_120)
    assert decision is not None
    assert decision.should_enter_prearm is True
    trigger_types = {reason.trigger_type for reason in decision.reasons}
    assert "slow_success" in trigger_types
    assert "retry_onset" in trigger_types


def test_same_mild_trigger_twice_does_not_count_as_two_distinct():
    config = make_config(
        enable_in_flight_pileup=False,
        enable_retry_onset=False,
        slow_success=SlowSuccessConfig(
            min_slow_duration_ns=250_000_000,
            slow_multiplier=1.1,
            ewma_alpha=0.1,
            high_rate=0.9,
            mild_rate=0.1,
            min_samples=10,
            include_4xx_as_success_like=True,
            min_baseline_ns=1_000_000,
            max_baseline_ns=60_000_000_000,
        ),
    )
    engine = TriggerEngine(config)

    sec = 1
    for _ in range(20):
        engine.on_request_complete(complete(duration_ns=100_000_000), sec, 1_000)
    for _ in range(3):
        engine.on_request_complete(complete(duration_ns=2_000_000_000), sec, 1_050)

    assert engine.evaluate(CaptureMode.NORMAL, 1_000, sec, 1_100) is None
