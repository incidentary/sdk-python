"""Bounded local pre-arm trigger engine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .downstream_edge_key import RetryKeyQuality
from .types import CaptureMode

TriggerType = Literal["slow_success", "in_flight_pileup", "retry_onset", "error_rate_5xx"]
TriggerSeverity = Literal["mild", "severe"]

WINDOW_BUCKETS = 10
WINDOW_SECONDS = 10
RATE_SCALE = 10_000
MILD_RING_SIZE = 32
MILD_WINDOW_MS = 10_000
RETRY_PROBE_LIMIT = 8
MAX_UINT16 = 65_535


@dataclass(frozen=True)
class TriggerReason:
    trigger_type: TriggerType
    severity: TriggerSeverity
    observed_value: float
    threshold_value: float
    observed_label: str
    threshold_label: str
    fired_at_unix_ms: int
    summary: str
    details: dict[str, float | int | str]


@dataclass(frozen=True)
class RequestSignal:
    kind: Literal["HTTP_IN", "HTTP_OUT"]
    status_code: int
    duration_ns: int
    cancelled: bool
    timed_out: bool
    outbound_retry_key_hash: int
    outbound_retry_key_quality: RetryKeyQuality
    explicit_retry_observed: bool | None


@dataclass
class TriggerDecision:
    should_enter_prearm: bool
    reasons: list[TriggerReason]


@dataclass
class TriggerEngineSnapshot:
    disabled: dict[str, bool]
    totals: dict[str, int]
    slow_success: dict[str, float | int | None]
    in_flight_pileup: dict[str, float | int | None]
    retry_onset: dict[str, float | int | None | dict[str, int]]
    last_trigger: TriggerReason | None
    recent_mild_trigger_count: int


@dataclass(frozen=True)
class SlowSuccessConfig:
    min_slow_duration_ns: int
    slow_multiplier: float
    ewma_alpha: float
    high_rate: float
    mild_rate: float
    min_samples: int
    include_4xx_as_success_like: bool
    min_baseline_ns: int
    max_baseline_ns: int


@dataclass(frozen=True)
class InFlightConfig:
    min_absolute_in_flight: int
    baseline_multiplier: float
    net_growth_min: int
    severe_hold_secs: int
    mild_hold_secs: int
    baseline_alpha: float


@dataclass(frozen=True)
class RetryConfig:
    retry_window_ms: int
    high_rate: float
    mild_rate: float
    min_total_attempts: int
    table_size: int


@dataclass(frozen=True)
class TriggerEngineConfig:
    enable_slow_success: bool
    enable_in_flight_pileup: bool
    enable_retry_onset: bool
    slow_success: SlowSuccessConfig
    in_flight: InFlightConfig
    retry: RetryConfig


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _to_rate_bps(value: float) -> int:
    return int(_clamp(round(value * RATE_SCALE), 0, RATE_SCALE))


def _rate_bps(part: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(part * RATE_SCALE // total)


def _bps_to_pct(bps: int) -> float:
    return bps / 100.0


class SlowSuccessTrigger:
    def __init__(self, cfg: SlowSuccessConfig):
        self._cfg = cfg
        self._total = [0] * WINDOW_BUCKETS
        self._slow = [0] * WINDOW_BUCKETS
        self._bucket_sec = [-1] * WINDOW_BUCKETS
        self._ewma_ns = 0.0
        self._last_severity: TriggerSeverity | None = None

    def on_request_complete(self, signal: RequestSignal, now_sec: int) -> None:
        if not self._is_success_like(signal):
            return

        duration = int(
            _clamp(signal.duration_ns, self._cfg.min_baseline_ns, self._cfg.max_baseline_ns)
        )
        baseline = self._ewma_ns if self._ewma_ns > 0 else duration
        slow_threshold = self._slow_threshold(int(baseline))

        self._update_ewma(duration)

        idx = now_sec % WINDOW_BUCKETS
        self._ensure_bucket(idx, now_sec)
        self._total[idx] += 1
        if signal.duration_ns > slow_threshold:
            self._slow[idx] += 1

    def evaluate(self, now_ms: int, now_sec: int) -> TriggerReason | None:
        total, slow, rate_bps = self._collect_window(now_sec)
        current = self._classify(total, slow)
        previous = self._last_severity
        self._last_severity = current

        if current is None:
            return None
        if previous == current:
            return None
        if previous == "severe":
            return None

        threshold_bps = _to_rate_bps(
            self._cfg.high_rate if current == "severe" else self._cfg.mild_rate
        )
        return TriggerReason(
            trigger_type="slow_success",
            severity=current,
            observed_value=rate_bps,
            threshold_value=threshold_bps,
            observed_label=f"{_bps_to_pct(rate_bps):.2f}% slow successes over 10s",
            threshold_label=f"{_bps_to_pct(threshold_bps):.2f}%",
            fired_at_unix_ms=now_ms,
            summary=(
                "pre-armed due to slow-success spike: "
                f"{_bps_to_pct(rate_bps):.2f}% slow successes over last 10s, "
                f"threshold {_bps_to_pct(threshold_bps):.2f}%"
            ),
            details={
                "total_success_like_10s": total,
                "slow_success_like_10s": slow,
                "slow_success_rate_pct": _bps_to_pct(rate_bps),
                "ewma_success_duration_ns": int(self._ewma_ns),
                "slow_threshold_ns": self._slow_threshold(
                    int(self._ewma_ns or self._cfg.min_slow_duration_ns)
                ),
            },
        )

    def snapshot(self, now_sec: int) -> dict[str, float | int | None]:
        total, slow, rate_bps = self._collect_window(now_sec)
        return {
            "severity": self._classify(total, slow),
            "total_success_like_10s": total,
            "slow_success_like_10s": slow,
            "slow_success_rate_pct": _bps_to_pct(rate_bps),
            "ewma_success_duration_ns": int(self._ewma_ns),
            "slow_threshold_ns": self._slow_threshold(
                int(self._ewma_ns or self._cfg.min_slow_duration_ns)
            ),
        }

    def reset(self) -> None:
        self._total = [0] * WINDOW_BUCKETS
        self._slow = [0] * WINDOW_BUCKETS
        self._bucket_sec = [-1] * WINDOW_BUCKETS
        self._ewma_ns = 0.0
        self._last_severity = None

    def _collect_window(self, now_sec: int) -> tuple[int, int, int]:
        total = 0
        slow = 0
        min_sec = now_sec - (WINDOW_SECONDS - 1)
        for i in range(WINDOW_BUCKETS):
            sec = self._bucket_sec[i]
            if min_sec <= sec <= now_sec:
                total += self._total[i]
                slow += self._slow[i]
        return total, slow, _rate_bps(slow, total)

    def _classify(self, total: int, slow: int) -> TriggerSeverity | None:
        if total < self._cfg.min_samples:
            return None
        rate = _rate_bps(slow, total)
        if rate >= _to_rate_bps(self._cfg.high_rate):
            return "severe"
        if rate >= _to_rate_bps(self._cfg.mild_rate):
            return "mild"
        return None

    def _ensure_bucket(self, idx: int, now_sec: int) -> None:
        if self._bucket_sec[idx] == now_sec:
            return
        self._bucket_sec[idx] = now_sec
        self._total[idx] = 0
        self._slow[idx] = 0

    def _is_success_like(self, signal: RequestSignal) -> bool:
        if signal.cancelled or signal.timed_out:
            return False
        if 200 <= signal.status_code < 400:
            return True
        return bool(self._cfg.include_4xx_as_success_like and 400 <= signal.status_code < 500)

    def _update_ewma(self, duration_ns: int) -> None:
        if self._ewma_ns <= 0:
            self._ewma_ns = float(duration_ns)
            return
        next_value = self._ewma_ns + self._cfg.ewma_alpha * (duration_ns - self._ewma_ns)
        self._ewma_ns = _clamp(next_value, self._cfg.min_baseline_ns, self._cfg.max_baseline_ns)

    def _slow_threshold(self, baseline_ns: int) -> int:
        return max(self._cfg.min_slow_duration_ns, round(baseline_ns * self._cfg.slow_multiplier))


class InFlightPileupTrigger:
    def __init__(self, cfg: InFlightConfig):
        self._cfg = cfg
        self._started = [0] * WINDOW_BUCKETS
        self._completed = [0] * WINDOW_BUCKETS
        self._peak = [0] * WINDOW_BUCKETS
        self._bucket_sec = [-1] * WINDOW_BUCKETS

        self._current_in_flight = 0
        self._ewma_peak = 0.0
        self._condition_start_mono: int | None = None
        self._persistence_ms = 0
        self._last_severity: TriggerSeverity | None = None

    def on_request_start(self, now_sec: int) -> None:
        idx = now_sec % WINDOW_BUCKETS
        self._ensure_bucket(idx, now_sec)
        self._current_in_flight += 1
        self._started[idx] += 1
        if self._current_in_flight > self._peak[idx]:
            self._peak[idx] = self._current_in_flight

    def on_request_complete(self, now_sec: int) -> None:
        idx = now_sec % WINDOW_BUCKETS
        self._ensure_bucket(idx, now_sec)
        self._current_in_flight = max(0, self._current_in_flight - 1)
        self._completed[idx] += 1

    def evaluate(
        self, mode: CaptureMode, now_ms: int, now_sec: int, mono_ms: int
    ) -> TriggerReason | None:
        started, completed, net_growth, peak_10s = self._collect_window(now_sec)
        _ = started, completed
        threshold = self._threshold()
        condition_met = (
            self._current_in_flight >= threshold and net_growth >= self._cfg.net_growth_min
        )

        if condition_met:
            if self._condition_start_mono is None:
                self._condition_start_mono = mono_ms
            self._persistence_ms = max(0, mono_ms - self._condition_start_mono)
        else:
            self._condition_start_mono = None
            self._persistence_ms = 0

        current = self._classify(condition_met, self._persistence_ms)
        previous = self._last_severity
        self._last_severity = current

        if mode == CaptureMode.NORMAL:
            self._update_baseline(peak_10s)

        if current is None:
            return None
        if previous == current:
            return None
        if previous == "severe":
            return None

        return TriggerReason(
            trigger_type="in_flight_pileup",
            severity=current,
            observed_value=float(self._current_in_flight),
            threshold_value=float(threshold),
            observed_label=f"{self._current_in_flight} in-flight",
            threshold_label=f"{threshold} in-flight threshold",
            fired_at_unix_ms=now_ms,
            summary=(
                "pre-armed due to in-flight pileup: "
                f"current={self._current_in_flight}, baseline={round(self._ewma_peak)}, "
                f"net growth={net_growth} over last 10s"
            ),
            details={
                "current_in_flight": self._current_in_flight,
                "peak_in_flight_10s": peak_10s,
                "net_growth_10s": net_growth,
                "ewma_peak_in_flight": round(self._ewma_peak),
                "inflight_threshold": threshold,
                "persistence_ms": self._persistence_ms,
            },
        )

    def snapshot(self, now_sec: int) -> dict[str, float | int | None]:
        _, _, net_growth, peak_10s = self._collect_window(now_sec)
        threshold = self._threshold()
        condition_met = (
            self._current_in_flight >= threshold and net_growth >= self._cfg.net_growth_min
        )
        return {
            "severity": self._classify(condition_met, self._persistence_ms),
            "current_in_flight": self._current_in_flight,
            "peak_in_flight_10s": peak_10s,
            "net_growth_10s": net_growth,
            "ewma_peak_in_flight": round(self._ewma_peak),
            "inflight_threshold": threshold,
            "persistence_ms": self._persistence_ms,
        }

    def reset(self) -> None:
        self._started = [0] * WINDOW_BUCKETS
        self._completed = [0] * WINDOW_BUCKETS
        self._peak = [0] * WINDOW_BUCKETS
        self._bucket_sec = [-1] * WINDOW_BUCKETS
        self._current_in_flight = 0
        self._ewma_peak = 0.0
        self._condition_start_mono = None
        self._persistence_ms = 0
        self._last_severity = None

    def _collect_window(self, now_sec: int) -> tuple[int, int, int, int]:
        started = 0
        completed = 0
        peak = 0
        min_sec = now_sec - (WINDOW_SECONDS - 1)
        for i in range(WINDOW_BUCKETS):
            sec = self._bucket_sec[i]
            if min_sec <= sec <= now_sec:
                started += self._started[i]
                completed += self._completed[i]
                peak = max(peak, self._peak[i])
        return started, completed, started - completed, peak

    def _ensure_bucket(self, idx: int, now_sec: int) -> None:
        if self._bucket_sec[idx] == now_sec:
            return
        self._bucket_sec[idx] = now_sec
        self._started[idx] = 0
        self._completed[idx] = 0
        self._peak[idx] = 0

    def _threshold(self) -> int:
        return max(
            self._cfg.min_absolute_in_flight,
            round(self._ewma_peak * self._cfg.baseline_multiplier),
        )

    def _classify(self, condition_met: bool, persistence_ms: int) -> TriggerSeverity | None:
        if not condition_met:
            return None
        persistence_secs = persistence_ms / 1000.0
        if persistence_secs >= self._cfg.severe_hold_secs:
            return "severe"
        if persistence_secs >= self._cfg.mild_hold_secs:
            return "mild"
        return None

    def _update_baseline(self, peak_in_flight: int) -> None:
        if peak_in_flight <= 0:
            return
        if self._ewma_peak <= 0:
            self._ewma_peak = float(peak_in_flight)
            return
        self._ewma_peak = self._ewma_peak + self._cfg.baseline_alpha * (
            peak_in_flight - self._ewma_peak
        )


class RetryOnsetTrigger:
    _qualities: list[RetryKeyQuality] = [
        "explicit",
        "route_template",
        "logical_edge",
        "normalized_url",
        "unknown",
    ]

    def __init__(self, cfg: RetryConfig):
        self._cfg = cfg
        self._total = [0] * WINDOW_BUCKETS
        self._retries = [0] * WINDOW_BUCKETS
        self._bucket_sec = [-1] * WINDOW_BUCKETS

        self._quality_buckets: dict[RetryKeyQuality, list[int]] = {
            quality: [0] * WINDOW_BUCKETS for quality in self._qualities
        }
        self._quality_totals: dict[RetryKeyQuality, int] = {
            quality: 0 for quality in self._qualities
        }

        table_size = _next_power_of_two(max(128, cfg.table_size))
        self._table_size = table_size
        self._table_mask = table_size - 1
        self._key_hash = [0] * table_size
        self._last_seen = [0] * table_size
        self._attempts = [0] * table_size
        self._occupied = [False] * table_size

        self._collisions = 0
        self._replacements = 0
        self._occupancy = 0
        self._last_severity: TriggerSeverity | None = None

    def on_request_complete(self, signal: RequestSignal, now_sec: int, mono_ms: int) -> None:
        if signal.kind != "HTTP_OUT":
            return

        idx = now_sec % WINDOW_BUCKETS
        self._ensure_bucket(idx, now_sec)
        self._total[idx] += 1
        self._quality_buckets[signal.outbound_retry_key_quality][idx] += 1
        self._quality_totals[signal.outbound_retry_key_quality] += 1

        retry_observed = False
        if signal.explicit_retry_observed is True:
            retry_observed = True
        elif signal.explicit_retry_observed is None and signal.outbound_retry_key_hash != 0:
            retry_observed = self._observe_heuristic_retry(signal.outbound_retry_key_hash, mono_ms)

        if retry_observed:
            self._retries[idx] += 1

    def evaluate(self, now_ms: int, now_sec: int) -> TriggerReason | None:
        total, retries, rate_bps, fallback_rate, _ = self._collect_window(now_sec)
        current = self._classify(total, retries)
        previous = self._last_severity
        self._last_severity = current

        if current is None:
            return None
        if previous == current:
            return None
        if previous == "severe":
            return None

        threshold = _to_rate_bps(
            self._cfg.high_rate if current == "severe" else self._cfg.mild_rate
        )
        return TriggerReason(
            trigger_type="retry_onset",
            severity=current,
            observed_value=float(rate_bps),
            threshold_value=float(threshold),
            observed_label=f"{_bps_to_pct(rate_bps):.2f}% retries over 10s",
            threshold_label=f"{_bps_to_pct(threshold):.2f}%",
            fired_at_unix_ms=now_ms,
            summary=(
                "pre-armed due to retry onset: "
                f"{_bps_to_pct(rate_bps):.2f}% retries over last 10s, "
                f"threshold {_bps_to_pct(threshold):.2f}%"
            ),
            details={
                "total_outbound_attempts_10s": total,
                "retry_observations_10s": retries,
                "retry_rate_pct": _bps_to_pct(rate_bps),
                "retry_normalized_url_fallback_rate_10s": fallback_rate,
                "retry_table_load_factor": self._occupancy / self._table_size
                if self._table_size
                else 0.0,
                "collision_count": self._collisions,
                "replacement_count": self._replacements,
            },
        )

    def snapshot(self, now_sec: int) -> dict[str, float | int | None | dict[str, int]]:
        total, retries, rate_bps, fallback_rate, quality_10s = self._collect_window(now_sec)
        return {
            "severity": self._classify(total, retries),
            "total_outbound_attempts_10s": total,
            "retry_observations_10s": retries,
            "retry_rate_pct": _bps_to_pct(rate_bps),
            "normalized_url_fallback_rate_10s": fallback_rate,
            "retry_key_quality_10s": quality_10s,
            "retry_key_quality_total": dict(self._quality_totals),
            "retry_table_load_factor": self._occupancy / self._table_size
            if self._table_size
            else 0.0,
            "collision_count": self._collisions,
            "replacement_count": self._replacements,
        }

    def reset(self) -> None:
        self._total = [0] * WINDOW_BUCKETS
        self._retries = [0] * WINDOW_BUCKETS
        self._bucket_sec = [-1] * WINDOW_BUCKETS
        self._quality_buckets = {quality: [0] * WINDOW_BUCKETS for quality in self._qualities}
        self._quality_totals = {quality: 0 for quality in self._qualities}
        self._key_hash = [0] * self._table_size
        self._last_seen = [0] * self._table_size
        self._attempts = [0] * self._table_size
        self._occupied = [False] * self._table_size
        self._collisions = 0
        self._replacements = 0
        self._occupancy = 0
        self._last_severity = None

    def _collect_window(
        self, now_sec: int
    ) -> tuple[int, int, int, float, dict[RetryKeyQuality, int]]:
        total = 0
        retries = 0
        min_sec = now_sec - (WINDOW_SECONDS - 1)
        quality: dict[RetryKeyQuality, int] = {q: 0 for q in self._qualities}

        for i in range(WINDOW_BUCKETS):
            sec = self._bucket_sec[i]
            if min_sec <= sec <= now_sec:
                total += self._total[i]
                retries += self._retries[i]
                for q in self._qualities:
                    quality[q] += self._quality_buckets[q][i]

        fallback_rate = quality["normalized_url"] / total if total > 0 else 0.0
        return total, retries, _rate_bps(retries, total), fallback_rate, quality

    def _classify(self, total: int, retries: int) -> TriggerSeverity | None:
        if total < self._cfg.min_total_attempts:
            return None
        rate = _rate_bps(retries, total)
        if rate >= _to_rate_bps(self._cfg.high_rate):
            return "severe"
        if rate >= _to_rate_bps(self._cfg.mild_rate):
            return "mild"
        return None

    def _observe_heuristic_retry(self, key_hash: int, now_ms: int) -> bool:
        start = key_hash & self._table_mask
        empty = -1
        stale = -1
        stalest = start
        stalest_age = -1

        for offset in range(RETRY_PROBE_LIMIT):
            idx = (start + offset) & self._table_mask
            if not self._occupied[idx]:
                empty = idx
                break

            age = now_ms - self._last_seen[idx]
            if self._key_hash[idx] == key_hash:
                if age <= self._cfg.retry_window_ms:
                    self._attempts[idx] = min(MAX_UINT16, self._attempts[idx] + 1)
                    self._last_seen[idx] = now_ms
                    return self._attempts[idx] >= 2

                self._attempts[idx] = 1
                self._last_seen[idx] = now_ms
                return False

            self._collisions += 1
            if age > self._cfg.retry_window_ms and stale < 0:
                stale = idx
            if age > stalest_age:
                stalest_age = age
                stalest = idx

        target = empty if empty >= 0 else stale if stale >= 0 else stalest
        if not self._occupied[target]:
            self._occupancy += 1
        else:
            self._replacements += 1

        self._occupied[target] = True
        self._key_hash[target] = key_hash
        self._last_seen[target] = now_ms
        self._attempts[target] = 1
        return False

    def _ensure_bucket(self, idx: int, now_sec: int) -> None:
        if self._bucket_sec[idx] == now_sec:
            return
        self._bucket_sec[idx] = now_sec
        self._total[idx] = 0
        self._retries[idx] = 0
        for quality in self._qualities:
            self._quality_buckets[quality][idx] = 0


class TriggerEngine:
    def __init__(self, cfg: TriggerEngineConfig):
        self._cfg = cfg
        self._slow = SlowSuccessTrigger(cfg.slow_success)
        self._inflight = InFlightPileupTrigger(cfg.in_flight)
        self._retry = RetryOnsetTrigger(cfg.retry)

        self._disabled = {
            "slow_success": False,
            "in_flight_pileup": False,
            "retry_onset": False,
        }
        self._disabled_logged = {
            "slow_success": False,
            "in_flight_pileup": False,
            "retry_onset": False,
        }

        self._mild_types = [""] * MILD_RING_SIZE
        self._mild_at_ms = [0] * MILD_RING_SIZE
        self._mild_reasons: list[TriggerReason | None] = [None] * MILD_RING_SIZE
        self._mild_write = 0

        self._totals = {
            "prearm_trigger_slow_success_total": 0,
            "prearm_trigger_inflight_pileup_total": 0,
            "prearm_trigger_retry_onset_total": 0,
        }
        self._last_trigger: TriggerReason | None = None

    def on_request_start(self, now_sec: int) -> None:
        if self._cfg.enable_in_flight_pileup and not self._disabled["in_flight_pileup"]:
            self._safe_run("in_flight_pileup", lambda: self._inflight.on_request_start(now_sec))

    def on_request_complete(self, signal: RequestSignal, now_sec: int, mono_ms: int) -> None:
        if self._cfg.enable_slow_success and not self._disabled["slow_success"]:
            self._safe_run("slow_success", lambda: self._slow.on_request_complete(signal, now_sec))

        if self._cfg.enable_in_flight_pileup and not self._disabled["in_flight_pileup"]:
            self._safe_run("in_flight_pileup", lambda: self._inflight.on_request_complete(now_sec))

        if self._cfg.enable_retry_onset and not self._disabled["retry_onset"]:
            self._safe_run(
                "retry_onset", lambda: self._retry.on_request_complete(signal, now_sec, mono_ms)
            )

    def evaluate(
        self, mode: CaptureMode, now_ms: int, now_sec: int, mono_ms: int
    ) -> TriggerDecision | None:
        severe: list[TriggerReason] = []

        def on_fire(reason: TriggerReason) -> None:
            self._last_trigger = reason
            if reason.trigger_type == "slow_success":
                self._totals["prearm_trigger_slow_success_total"] += 1
            elif reason.trigger_type == "in_flight_pileup":
                self._totals["prearm_trigger_inflight_pileup_total"] += 1
            elif reason.trigger_type == "retry_onset":
                self._totals["prearm_trigger_retry_onset_total"] += 1

            if reason.severity == "severe":
                severe.append(reason)
                return

            i = self._mild_write
            self._mild_types[i] = reason.trigger_type
            self._mild_at_ms[i] = mono_ms
            self._mild_reasons[i] = reason
            self._mild_write = (self._mild_write + 1) % MILD_RING_SIZE

        if self._cfg.enable_slow_success and not self._disabled["slow_success"]:
            self._safe_run(
                "slow_success",
                lambda: self._fire_if(self._slow.evaluate(now_ms, now_sec), on_fire),
            )

        if self._cfg.enable_in_flight_pileup and not self._disabled["in_flight_pileup"]:
            self._safe_run(
                "in_flight_pileup",
                lambda: self._fire_if(
                    self._inflight.evaluate(mode, now_ms, now_sec, mono_ms), on_fire
                ),
            )

        if self._cfg.enable_retry_onset and not self._disabled["retry_onset"]:
            self._safe_run(
                "retry_onset",
                lambda: self._fire_if(self._retry.evaluate(now_ms, now_sec), on_fire),
            )

        if severe:
            return TriggerDecision(should_enter_prearm=True, reasons=severe)

        mild_distinct = self._collect_distinct_mild(mono_ms)
        if len(mild_distinct) >= 2:
            return TriggerDecision(should_enter_prearm=True, reasons=mild_distinct)

        return None

    def snapshot(self, now_sec: int, mono_ms: int) -> TriggerEngineSnapshot:
        mild = self._collect_distinct_mild(mono_ms)
        return TriggerEngineSnapshot(
            disabled=dict(self._disabled),
            totals=dict(self._totals),
            slow_success=self._slow.snapshot(now_sec),
            in_flight_pileup=self._inflight.snapshot(now_sec),
            retry_onset=self._retry.snapshot(now_sec),
            last_trigger=self._last_trigger,
            recent_mild_trigger_count=len(mild),
        )

    def reset_for_tests(self) -> None:
        self._slow.reset()
        self._inflight.reset()
        self._retry.reset()
        self._disabled = {"slow_success": False, "in_flight_pileup": False, "retry_onset": False}
        self._disabled_logged = {
            "slow_success": False,
            "in_flight_pileup": False,
            "retry_onset": False,
        }
        self._mild_types = [""] * MILD_RING_SIZE
        self._mild_at_ms = [0] * MILD_RING_SIZE
        self._mild_reasons = [None] * MILD_RING_SIZE
        self._mild_write = 0
        self._totals = {
            "prearm_trigger_slow_success_total": 0,
            "prearm_trigger_inflight_pileup_total": 0,
            "prearm_trigger_retry_onset_total": 0,
        }
        self._last_trigger = None

    @staticmethod
    def _fire_if(reason: TriggerReason | None, sink) -> None:
        if reason is not None:
            sink(reason)

    def _collect_distinct_mild(self, mono_ms: int) -> list[TriggerReason]:
        latest: dict[TriggerType, tuple[int, TriggerReason]] = {}
        for i in range(MILD_RING_SIZE):
            trigger_type = self._mild_types[i]
            if not trigger_type:
                continue
            at_ms = self._mild_at_ms[i]
            if mono_ms - at_ms > MILD_WINDOW_MS:
                continue
            reason = self._mild_reasons[i]
            if reason is None:
                continue
            prev = latest.get(trigger_type)  # type: ignore[arg-type]
            if prev is None or at_ms > prev[0]:
                latest[trigger_type] = (at_ms, reason)  # type: ignore[index]

        if len(latest) < 2:
            return []

        return [item[1] for item in latest.values()]

    def _safe_run(
        self, trigger: Literal["slow_success", "in_flight_pileup", "retry_onset"], fn
    ) -> None:
        try:
            fn()
        except Exception as exc:
            self._disabled[trigger] = True
            if not self._disabled_logged[trigger]:
                self._disabled_logged[trigger] = True
                print(f"[incidentary] disabling {trigger} trigger after internal failure: {exc}")


def _next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (math.ceil(math.log2(value)))
