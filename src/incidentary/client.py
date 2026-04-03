"""Incidentary Python SDK client."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from .integrations import Integration, IntegrationRegistry, default_integrations
from .prearm_triggers import (
    InFlightConfig,
    RequestSignal,
    RetryConfig,
    SlowSuccessConfig,
    TriggerEngine,
    TriggerEngineConfig,
    TriggerReason,
)
from .ring_buffer import RingBuffer
from .transport import Transport
from .types import (
    CaptureMode,
    CeDetail,
    IncidentaryEventType,
    PreArmTriggerReason,
    PreArmWindow,
    RecordEventOptions,
    RecordRequestOptions,
    SkeletonCe,
)

logger = logging.getLogger("incidentary.client")

_DEFAULT_DETAIL_REQUEST_HEADER_ALLOWLIST = [
    "content-type",
    "content-length",
    "user-agent",
    "x-request-id",
]
_DEFAULT_DETAIL_RESPONSE_HEADER_ALLOWLIST = [
    "content-type",
    "content-length",
    "x-request-id",
]
_DEFAULT_REDACT_FIELDS = [
    "password",
    "token",
    "authorization",
    "credit_card",
    "ssn",
    "email",
    "phone",
]


@dataclass(frozen=True)
class _Clock:
    wall_ms: int
    wall_sec: int
    mono_ms: int


class _ShardedCounter:
    """Fixed-shard counter for low-contention increments."""

    def __init__(self, shards: int = 16):
        size = 1
        while size < max(1, shards):
            size <<= 1
        self._mask = size - 1
        self._values = [0] * size
        self._locks = [threading.Lock() for _ in range(size)]
        self._probe_span = min(4, size)

    def add(self, delta: int = 1) -> int:
        start = threading.get_ident() & self._mask
        for offset in range(self._probe_span):
            index = (start + offset) & self._mask
            lock = self._locks[index]
            if lock.acquire(blocking=False):
                try:
                    self._values[index] += delta
                    return self._values[index]
                finally:
                    lock.release()

        lock = self._locks[start]
        with lock:
            self._values[start] += delta
            return self._values[start]

    def drain(self) -> int:
        total = 0
        for index, lock in enumerate(self._locks):
            if not lock.acquire(blocking=False):
                continue
            try:
                total += self._values[index]
                self._values[index] = 0
            finally:
                lock.release()
        return total


class _RollingWindow:
    """10-bucket rolling error rate window."""

    def __init__(self, window_ms: int = 10_000, buckets: int = 10):
        self._bucket_ms = max(1, window_ms // max(1, buckets))
        self._buckets = [{"total": 0, "errors": 0} for _ in range(max(1, buckets))]
        self._head = 0
        self._last_ms = int(time.time() * 1000)

    def record(self, is_error: bool, now_ms: int) -> None:
        self._advance(now_ms)
        bucket = self._buckets[self._head]
        bucket["total"] += 1
        if is_error:
            bucket["errors"] += 1

    def error_rate_pct(self, now_ms: int) -> float:
        self._advance(now_ms)
        total = sum(bucket["total"] for bucket in self._buckets)
        errors = sum(bucket["errors"] for bucket in self._buckets)
        return (errors / total * 100.0) if total > 0 else 0.0

    def _advance(self, now_ms: int) -> None:
        steps = min((now_ms - self._last_ms) // self._bucket_ms, len(self._buckets))
        for _ in range(steps):
            self._head = (self._head + 1) % len(self._buckets)
            self._buckets[self._head] = {"total": 0, "errors": 0}
        if steps:
            self._last_ms = now_ms


class IncidentaryClient:
    """
    Python Incidentary SDK client.

    State machine: NORMAL -> PRE_ARMED -> INCIDENT -> NORMAL
    Thread-safe for concurrent request handling.
    Never raises in record/write paths.
    """

    def __init__(
        self,
        api_key: str,
        service_name: str,
        api_url: str | None = None,
        base_url: str | None = None,
        environment: str = "production",
        workspace_id: str = "",
        pre_arm_threshold_high: float = 10.0,
        pre_arm_threshold_low: float = 2.0,
        pre_arm_min_duration_ms: int = 60_000,
        pre_arm_ttl_ms: int = 300_000,
        pre_arm_cooldown_ms: int = 30_000,
        buffer_capacity: int = 4_000,
        pre_arm_enable_slow_success: bool = True,
        pre_arm_enable_inflight: bool = True,
        pre_arm_enable_retry: bool = True,
        pre_arm_slow_min_ms: int = 250,
        pre_arm_slow_multiplier: float = 2.0,
        pre_arm_slow_alpha: float = 0.1,
        pre_arm_slow_success_rate_high: float = 0.20,
        pre_arm_slow_success_rate_mild: float = 0.10,
        pre_arm_slow_min_samples: int = 50,
        pre_arm_slow_include_4xx_as_success_like: bool = True,
        pre_arm_inflight_min_abs: int = 32,
        pre_arm_inflight_multiplier: float = 2.0,
        pre_arm_inflight_net_growth_min: int = 16,
        pre_arm_inflight_hold_secs: int = 3,
        pre_arm_inflight_mild_hold_secs: int = 2,
        pre_arm_retry_window_ms: int = 5_000,
        pre_arm_retry_rate_high: float = 0.10,
        pre_arm_retry_rate_mild: float = 0.05,
        pre_arm_retry_min_total: int = 20,
        pre_arm_retry_table_size: int = 4_096,
        pre_arm_detail_capture_enabled: bool = True,
        pre_arm_detail_capture_payload_enabled: bool = False,
        pre_arm_detail_max_payload_bytes: int = 4_096,
        pre_arm_detail_request_header_allowlist: Iterable[str] | None = None,
        pre_arm_detail_response_header_allowlist: Iterable[str] | None = None,
        redact_fields: Iterable[str] | None = None,
        pre_arm_eval_shards: int = 16,
        pre_arm_eval_batch_size: int = 1,
        timeout_ms: int = 5_000,
        on_error: Callable[[Exception], None] | None = None,
        auto_instrument: bool = True,
        integrations: list[Integration] | None = None,
    ):
        resolved_base_url = (base_url or api_url or "").strip().rstrip("/")
        self.service_name = service_name
        self._api_key = api_key
        self._api_url = resolved_base_url
        self._environment = environment
        self._workspace_id = workspace_id

        self._threshold_high = pre_arm_threshold_high
        self._threshold_low = pre_arm_threshold_low
        self._min_duration = pre_arm_min_duration_ms
        self._ttl = pre_arm_ttl_ms
        self._cooldown = pre_arm_cooldown_ms

        self._mode = CaptureMode.NORMAL
        self._window = _RollingWindow()
        self._buffer = RingBuffer(capacity=buffer_capacity)
        self._trigger_engine = TriggerEngine(
            TriggerEngineConfig(
                enable_slow_success=pre_arm_enable_slow_success,
                enable_in_flight_pileup=pre_arm_enable_inflight,
                enable_retry_onset=pre_arm_enable_retry,
                slow_success=SlowSuccessConfig(
                    min_slow_duration_ns=max(1, pre_arm_slow_min_ms) * 1_000_000,
                    slow_multiplier=max(0.01, pre_arm_slow_multiplier),
                    ewma_alpha=max(0.001, min(1.0, pre_arm_slow_alpha)),
                    high_rate=max(0.0, min(1.0, pre_arm_slow_success_rate_high)),
                    mild_rate=max(0.0, min(1.0, pre_arm_slow_success_rate_mild)),
                    min_samples=max(1, pre_arm_slow_min_samples),
                    include_4xx_as_success_like=pre_arm_slow_include_4xx_as_success_like,
                    min_baseline_ns=1_000_000,
                    max_baseline_ns=60_000_000_000,
                ),
                in_flight=InFlightConfig(
                    min_absolute_in_flight=max(1, pre_arm_inflight_min_abs),
                    baseline_multiplier=max(0.1, pre_arm_inflight_multiplier),
                    net_growth_min=max(1, pre_arm_inflight_net_growth_min),
                    severe_hold_secs=max(1, pre_arm_inflight_hold_secs),
                    mild_hold_secs=max(1, pre_arm_inflight_mild_hold_secs),
                    baseline_alpha=0.05,
                ),
                retry=RetryConfig(
                    retry_window_ms=max(1, pre_arm_retry_window_ms),
                    high_rate=max(0.0, min(1.0, pre_arm_retry_rate_high)),
                    mild_rate=max(0.0, min(1.0, pre_arm_retry_rate_mild)),
                    min_total_attempts=max(1, pre_arm_retry_min_total),
                    table_size=max(128, pre_arm_retry_table_size),
                ),
            )
        )

        self._transport = Transport(
            base_url=resolved_base_url,
            api_url=api_url,
            api_key=api_key,
            service_name=service_name,
            environment=environment,
            workspace_id=workspace_id,
            timeout_ms=timeout_ms,
            on_error=on_error,
        )

        self._detail_capture_enabled = pre_arm_detail_capture_enabled
        self._detail_capture_payload_enabled = pre_arm_detail_capture_payload_enabled
        self._detail_max_payload_bytes = max(0, pre_arm_detail_max_payload_bytes)
        self._detail_request_header_allowlist = [
            item.lower().strip()
            for item in (
                pre_arm_detail_request_header_allowlist
                if pre_arm_detail_request_header_allowlist is not None
                else _DEFAULT_DETAIL_REQUEST_HEADER_ALLOWLIST
            )
            if item is not None and str(item).strip()
        ]
        self._detail_response_header_allowlist = [
            item.lower().strip()
            for item in (
                pre_arm_detail_response_header_allowlist
                if pre_arm_detail_response_header_allowlist is not None
                else _DEFAULT_DETAIL_RESPONSE_HEADER_ALLOWLIST
            )
            if item is not None and str(item).strip()
        ]
        self._redact_fields = {
            field.lower().strip()
            for field in (redact_fields if redact_fields is not None else _DEFAULT_REDACT_FIELDS)
            if field is not None and str(field).strip()
        }

        self._lock = threading.Lock()
        self._pre_arm_started_at: int | None = None
        self._pre_arm_timer: threading.Timer | None = None
        self._last_pre_arm_ended_at: int | None = None
        self._pre_arm_window_seq = 0
        self._pre_arm_alerted_at_ns: int | None = None
        self._pre_arm_ring_buffer_seq = 0

        self._active_pre_arm_window: PreArmWindow | None = None
        self._recent_pre_arm_windows: list[PreArmWindow | None] = [None] * 8
        self._recent_pre_arm_write_index = 0

        self._pre_arm_enter_total = 0
        self._pre_arm_bind_total = 0
        self._pre_arm_expire_total = 0
        self._pending_eval = _ShardedCounter(shards=pre_arm_eval_shards)
        self._eval_batch_size = max(1, pre_arm_eval_batch_size)
        self._last_eval_wall_ms = 0

        self._registry: IntegrationRegistry | None = None
        if auto_instrument:
            try:
                active_integrations = (
                    integrations if integrations is not None else default_integrations()
                )
                registry = IntegrationRegistry()
                for integration in active_integrations:
                    registry.register(integration)
                registry.discover_and_patch(self)
                self._registry = registry
            except Exception:
                logger.warning("Auto-instrumentation setup failed", exc_info=True)

    def get_capture_mode(self) -> CaptureMode:
        return self._mode

    def get_prearm_debug_state(self) -> dict[str, object]:
        try:
            with self._lock:
                clock = self._now_clock()
                snapshot = self._trigger_engine.snapshot(clock.wall_sec, clock.mono_ms)
                active = self._window_to_dict(self._active_pre_arm_window)
                recent = [
                    self._window_to_dict(window)
                    for window in self._recent_pre_arm_windows
                    if window is not None
                ]

                return {
                    "counters": {
                        "prearm_trigger_slow_success_total": snapshot.totals[
                            "prearm_trigger_slow_success_total"
                        ],
                        "prearm_trigger_inflight_pileup_total": snapshot.totals[
                            "prearm_trigger_inflight_pileup_total"
                        ],
                        "prearm_trigger_retry_onset_total": snapshot.totals[
                            "prearm_trigger_retry_onset_total"
                        ],
                        "prearm_enter_total": self._pre_arm_enter_total,
                        "prearm_bind_total": self._pre_arm_bind_total,
                        "prearm_expire_total": self._pre_arm_expire_total,
                    },
                    "gauges": {
                        "current_prearm_state": self._mode.value,
                        "current_in_flight": snapshot.in_flight_pileup.get("current_in_flight", 0),
                        "slow_success_rate_10s": snapshot.slow_success.get(
                            "slow_success_rate_pct", 0.0
                        ),
                        "retry_rate_10s": snapshot.retry_onset.get("retry_rate_pct", 0.0),
                        "retry_normalized_url_fallback_rate_10s": snapshot.retry_onset.get(
                            "normalized_url_fallback_rate_10s", 0.0
                        ),
                        "current_trigger_reasons_count": len(self._active_pre_arm_window.reasons)
                        if self._active_pre_arm_window is not None
                        else 0,
                    },
                    "retry_key_quality_10s": snapshot.retry_onset.get("retry_key_quality_10s", {}),
                    "retry_key_quality_total": snapshot.retry_onset.get(
                        "retry_key_quality_total", {}
                    ),
                    "last_trigger": {
                        "last_trigger_type": snapshot.last_trigger.trigger_type
                        if snapshot.last_trigger is not None
                        else None,
                        "last_trigger_severity": snapshot.last_trigger.severity
                        if snapshot.last_trigger is not None
                        else None,
                        "last_trigger_observed_value": snapshot.last_trigger.observed_value
                        if snapshot.last_trigger is not None
                        else None,
                        "last_trigger_threshold": snapshot.last_trigger.threshold_value
                        if snapshot.last_trigger is not None
                        else None,
                        "last_trigger_timestamp": snapshot.last_trigger.fired_at_unix_ms
                        if snapshot.last_trigger is not None
                        else None,
                    },
                    "active_prearm_window": active,
                    "recent_prearm_windows": recent,
                    "trigger_engine_disabled": snapshot.disabled,
                }
        except Exception:
            return {
                "counters": {},
                "gauges": {},
                "retry_key_quality_10s": {},
                "retry_key_quality_total": {},
                "last_trigger": {},
                "active_prearm_window": None,
                "recent_prearm_windows": [],
                "trigger_engine_disabled": {},
            }

    def write_event(self, ce: SkeletonCe) -> None:
        try:
            with self._lock:
                self._buffer.write(ce)
        except Exception:
            pass

    def flush_to_backend(self, incident_id: str | None = None) -> None:
        try:
            with self._lock:
                mode = "FULL" if self._mode != CaptureMode.NORMAL else "SKELETON"
                events = self._annotate_buffered_events_locked(self._buffer.flush())
            self._transport.upload_batch(events, capture_mode=mode, incident_id=incident_id)
        except Exception:
            pass

    def record_request_start(self, kind: str = "HTTP_IN") -> None:
        try:
            clock = self._now_clock()
            self._trigger_engine.on_request_start(clock.wall_sec)
            self._schedule_evaluation(kind, force=False)
        except Exception:
            pass

    def record_request(
        self, status_code: int, options: RecordRequestOptions | dict | None = None
    ) -> None:
        try:
            opts = self._normalize_request_options(options)
            clock = self._now_clock()
            self._window.record(status_code >= 500, clock.wall_ms)
            signal = RequestSignal(
                kind=opts.kind,
                status_code=status_code,
                duration_ns=max(0, opts.duration_ns),
                cancelled=opts.cancelled,
                timed_out=opts.timed_out,
                outbound_retry_key_hash=max(0, int(opts.outbound_retry_key_hash)),
                outbound_retry_key_quality=opts.outbound_retry_key_quality,
                explicit_retry_observed=opts.explicit_retry_observed,
            )
            self._trigger_engine.on_request_complete(signal, clock.wall_sec, clock.mono_ms)
            self._schedule_evaluation(signal.kind, force=(status_code >= 500))
        except Exception:
            pass

    def record_event(
        self,
        event_type: IncidentaryEventType,
        options: RecordEventOptions | None = None,
    ) -> None:
        try:
            opts = options if options is not None else RecordEventOptions()
            ce = SkeletonCe(
                ce_id=str(uuid.uuid4()),
                trace_id=opts.trace_id or str(uuid.uuid4()),
                parent_ce_id=opts.parent_ce_id,
                service_id=self.service_name,
                wall_ts_ns=opts.wall_ts_ns
                if opts.wall_ts_ns is not None
                else int(time.time() * 1_000_000_000),
                kind=self._event_type_to_kind(event_type),
                event_type=event_type,
                event_class="causal",
                event_attrs=opts.event_attrs,
                status=opts.status
                if opts.status is not None
                else self._event_type_default_status(event_type),
                duration_ns=max(0, int(opts.duration_ns)),
                sdk_version="0.2.0",
            )
            self.write_event(ce)
        except Exception:
            pass

    def record_queue_publish(self, options: RecordEventOptions | None = None) -> None:
        self.record_event("queue_publish", options)

    def record_queue_consume(self, options: RecordEventOptions | None = None) -> None:
        self.record_event("queue_consume", options)

    def record_job_start(self, options: RecordEventOptions | None = None) -> None:
        self.record_event("job_start", options)

    def record_job_end(self, options: RecordEventOptions | None = None) -> None:
        self.record_event("job_end", options)

    def record_webhook_in(self, options: RecordEventOptions | None = None) -> None:
        self.record_event("webhook_in", options)

    def record_webhook_out(self, options: RecordEventOptions | None = None) -> None:
        self.record_event("webhook_out", options)

    def should_capture_detail_for_current_mode(self) -> bool:
        return self._detail_capture_enabled and self._mode != CaptureMode.NORMAL

    def get_detail_request_header_allowlist(self) -> list[str]:
        return list(self._detail_request_header_allowlist)

    def get_detail_response_header_allowlist(self) -> list[str]:
        return list(self._detail_response_header_allowlist)

    def attach_detail_to_event(self, ce: SkeletonCe, detail: CeDetail | None) -> SkeletonCe:
        if not self.should_capture_detail_for_current_mode() or detail is None:
            return ce

        materialized = CeDetail(**asdict(detail))
        if materialized.payload_snippet is not None:
            if not self._detail_capture_payload_enabled:
                materialized.payload_snippet = None
            else:
                materialized.payload_snippet = self._normalize_payload_snippet(
                    materialized.payload_snippet
                )

        if not self._detail_has_content(materialized):
            return ce

        ce.detail = materialized
        return ce

    def escalate_to_incident(self, incident_id: str | None = None) -> None:
        with self._lock:
            if self._mode != CaptureMode.INCIDENT:
                self._mode = CaptureMode.INCIDENT
                self._pre_arm_alerted_at_ns = int(time.time() * 1_000_000_000)
                self._cancel_pre_arm_timer_locked()

            if self._active_pre_arm_window is not None:
                self._active_pre_arm_window.bound_incident_id = (
                    incident_id
                    if incident_id is not None
                    else self._active_pre_arm_window.bound_incident_id
                )
                self._pre_arm_bind_total += 1

    def close_incident(self) -> None:
        with self._lock:
            clock = self._now_clock()
            self._mode = CaptureMode.NORMAL
            self._pre_arm_alerted_at_ns = None
            self._pre_arm_ring_buffer_seq = 0
            self._pre_arm_started_at = None
            self._last_pre_arm_ended_at = clock.wall_ms
            self._cancel_pre_arm_timer_locked()

            if self._active_pre_arm_window is not None:
                self._active_pre_arm_window.closed_at_ms = clock.wall_ms
                self._active_pre_arm_window.close_reason = "incident_close"
                self._push_recent_window_locked(self._active_pre_arm_window)
                self._active_pre_arm_window = None

    def _event_type_to_kind(self, event_type: IncidentaryEventType) -> str:
        if event_type in ("http_in", "webhook_in"):
            return "HTTP_IN"
        if event_type in ("http_out", "webhook_out"):
            return "HTTP_OUT"
        if event_type == "queue_publish":
            return "QUEUE_PUBLISH"
        if event_type == "queue_consume":
            return "QUEUE_CONSUME"
        return "INTERNAL"

    def _event_type_default_status(self, event_type: IncidentaryEventType) -> int:
        if event_type in ("http_in", "http_out", "webhook_in", "webhook_out"):
            return 200
        return 0

    def _evaluate_pre_arm_locked(self, clock: _Clock, boundary_kind: str) -> None:
        rate = self._window.error_rate_pct(clock.wall_ms)

        if self._mode == CaptureMode.NORMAL:
            trigger_decision = self._trigger_engine.evaluate(
                self._mode,
                now_ms=clock.wall_ms,
                now_sec=clock.wall_sec,
                mono_ms=clock.mono_ms,
            )
            legacy_reason = (
                self._build_legacy_5xx_reason(rate, clock.wall_ms)
                if rate >= self._threshold_high
                else None
            )

            if self._is_cooldown_active_locked(clock.wall_ms):
                return

            if legacy_reason is not None:
                self._enter_pre_arm_locked(
                    [legacy_reason], clock, f"legacy_5xx_{boundary_kind.lower()}"
                )
                return

            if trigger_decision is not None and trigger_decision.should_enter_prearm:
                self._enter_pre_arm_locked(
                    trigger_decision.reasons,
                    clock,
                    f"local_trigger_{boundary_kind.lower()}",
                )
            return

        self._trigger_engine.evaluate(
            self._mode,
            now_ms=clock.wall_ms,
            now_sec=clock.wall_sec,
            mono_ms=clock.mono_ms,
        )

        if self._mode == CaptureMode.PRE_ARMED:
            elapsed = clock.wall_ms - (self._pre_arm_started_at or clock.wall_ms)
            min_duration_satisfied = elapsed >= self._min_duration
            ttl_expired = elapsed >= self._ttl
            rate_recovered = rate < self._threshold_low

            if min_duration_satisfied and (ttl_expired or rate_recovered):
                close_reason = "ttl" if ttl_expired else "error_rate_recovered"
                self._exit_pre_arm_locked(clock, close_reason)

    def _enter_pre_arm_locked(
        self, reasons: list[TriggerReason], clock: _Clock, source: str
    ) -> None:
        self._mode = CaptureMode.PRE_ARMED
        self._pre_arm_alerted_at_ns = None
        self._pre_arm_ring_buffer_seq = 0
        self._pre_arm_started_at = clock.wall_ms
        self._pre_arm_enter_total += 1

        deduped = self._dedupe_reasons(reasons)
        window = PreArmWindow(
            id=f"pw_{clock.wall_ms}_{self._pre_arm_window_seq}",
            started_at_ms=clock.wall_ms,
            expires_at_ms=clock.wall_ms + self._ttl,
            reasons=deduped,
            bound_incident_id=None,
            closed_at_ms=None,
            close_reason=None,
        )
        self._pre_arm_window_seq += 1
        self._active_pre_arm_window = window

        self._schedule_pre_arm_recheck_locked(delay_ms=min(1_000, max(1, self._ttl)))

        self._transport.notify_backend(
            "pre_arm_start",
            self.service_name,
            {
                "window_id": window.id,
                "started_at_ms": window.started_at_ms,
                "expires_at_ms": window.expires_at_ms,
                "source": source,
                "reasons": [
                    {
                        "trigger_type": reason.trigger_type,
                        "severity": reason.severity,
                        "summary": reason.summary,
                        "observed_label": reason.observed_label,
                        "threshold_label": reason.threshold_label,
                    }
                    for reason in deduped
                ],
            },
        )

    def _exit_pre_arm_locked(self, clock: _Clock, reason: str = "manual") -> None:
        self._mode = CaptureMode.NORMAL
        self._pre_arm_alerted_at_ns = None
        self._pre_arm_ring_buffer_seq = 0
        self._pre_arm_started_at = None
        self._last_pre_arm_ended_at = clock.wall_ms
        self._pre_arm_expire_total += 1
        self._cancel_pre_arm_timer_locked()

        if self._active_pre_arm_window is not None:
            self._active_pre_arm_window.closed_at_ms = clock.wall_ms
            self._active_pre_arm_window.close_reason = reason  # type: ignore[assignment]
            ended = self._active_pre_arm_window
            self._push_recent_window_locked(ended)
            self._active_pre_arm_window = None

            self._transport.notify_backend(
                "pre_arm_end",
                self.service_name,
                {
                    "window_id": ended.id,
                    "started_at_ms": ended.started_at_ms,
                    "ended_at_ms": ended.closed_at_ms,
                    "close_reason": ended.close_reason,
                    "bound_incident_id": ended.bound_incident_id,
                },
            )
            return

        self._transport.notify_backend(
            "pre_arm_end",
            self.service_name,
            {
                "close_reason": reason,
            },
        )

    def _annotate_buffered_events_locked(self, events: list[SkeletonCe]) -> list[SkeletonCe]:
        if not events:
            return events

        if self._mode == CaptureMode.PRE_ARMED:
            for event in events:
                event.captured_before_alert = True
                event.ring_buffer_seq = self._pre_arm_ring_buffer_seq
                self._pre_arm_ring_buffer_seq += 1
            return events

        if self._mode != CaptureMode.INCIDENT or self._pre_arm_alerted_at_ns is None:
            return events

        for event in events:
            if event.wall_ts_ns > self._pre_arm_alerted_at_ns:
                continue
            event.captured_before_alert = True
            event.ring_buffer_seq = self._pre_arm_ring_buffer_seq
            self._pre_arm_ring_buffer_seq += 1

        return events

    def _schedule_pre_arm_recheck_locked(self, delay_ms: int) -> None:
        self._cancel_pre_arm_timer_locked()
        timer = threading.Timer(max(0.001, delay_ms / 1000.0), self._on_pre_arm_timer)
        timer.daemon = True
        self._pre_arm_timer = timer
        timer.start()

    def _on_pre_arm_timer(self) -> None:
        try:
            with self._lock:
                if self._mode != CaptureMode.PRE_ARMED:
                    return

                clock = self._now_clock()
                self._evaluate_pre_arm_locked(clock, "HTTP_IN")
                self._last_eval_wall_ms = clock.wall_ms

                if self._mode == CaptureMode.PRE_ARMED:
                    self._schedule_pre_arm_recheck_locked(delay_ms=1_000)
        except Exception:
            pass

    def _schedule_evaluation(self, boundary_kind: str, force: bool) -> None:
        shard_count = self._pending_eval.add(1)
        if not force and (shard_count % self._eval_batch_size != 0):
            return

        if not self._lock.acquire(blocking=False):
            return

        try:
            pending = self._pending_eval.drain()
            if pending <= 0 and not force:
                return

            clock = self._now_clock()
            self._evaluate_pre_arm_locked(clock, boundary_kind)
            self._last_eval_wall_ms = clock.wall_ms
        finally:
            self._lock.release()

    def _cancel_pre_arm_timer_locked(self) -> None:
        if self._pre_arm_timer is not None:
            self._pre_arm_timer.cancel()
            self._pre_arm_timer = None

    def _push_recent_window_locked(self, window: PreArmWindow) -> None:
        copy = PreArmWindow(
            id=window.id,
            started_at_ms=window.started_at_ms,
            expires_at_ms=window.expires_at_ms,
            reasons=list(window.reasons),
            bound_incident_id=window.bound_incident_id,
            closed_at_ms=window.closed_at_ms,
            close_reason=window.close_reason,
        )
        self._recent_pre_arm_windows[self._recent_pre_arm_write_index] = copy
        self._recent_pre_arm_write_index = (self._recent_pre_arm_write_index + 1) % len(
            self._recent_pre_arm_windows
        )

    def _build_legacy_5xx_reason(self, error_rate_pct: float, now_ms: int) -> TriggerReason:
        return TriggerReason(
            trigger_type="error_rate_5xx",
            severity="severe",
            observed_value=error_rate_pct,
            threshold_value=self._threshold_high,
            observed_label=f"{error_rate_pct:.2f}% 5xx over 10s",
            threshold_label=f"{self._threshold_high:.2f}%",
            fired_at_unix_ms=now_ms,
            summary=(
                "pre-armed due to 5xx spike: "
                f"{error_rate_pct:.2f}% errors over last 10s, threshold {self._threshold_high:.2f}%"
            ),
            details={
                "error_rate_pct": error_rate_pct,
                "threshold_pct": self._threshold_high,
            },
        )

    def _is_cooldown_active_locked(self, now_ms: int) -> bool:
        if self._last_pre_arm_ended_at is None:
            return False
        return now_ms - self._last_pre_arm_ended_at < self._cooldown

    @staticmethod
    def _normalize_request_options(
        options: RecordRequestOptions | dict | None,
    ) -> RecordRequestOptions:
        if options is None:
            return RecordRequestOptions()
        if isinstance(options, RecordRequestOptions):
            return options
        if isinstance(options, dict):
            return RecordRequestOptions(
                kind=options.get("kind", "HTTP_IN"),
                duration_ns=int(options.get("duration_ns", 0)),
                cancelled=bool(options.get("cancelled", False)),
                timed_out=bool(options.get("timed_out", options.get("timedOut", False))),
                outbound_retry_key_hash=int(
                    options.get(
                        "outbound_retry_key_hash",
                        options.get("outboundRetryKeyHash", 0),
                    )
                ),
                outbound_retry_key_quality=options.get(
                    "outbound_retry_key_quality",
                    options.get("outboundRetryKeyQuality", "unknown"),
                ),
                explicit_retry_observed=options.get(
                    "explicit_retry_observed",
                    options.get("explicitRetryObserved"),
                ),
            )
        return RecordRequestOptions()

    def _dedupe_reasons(self, reasons: list[TriggerReason]) -> list[PreArmTriggerReason]:
        latest: dict[str, TriggerReason] = {}
        for reason in reasons:
            previous = latest.get(reason.trigger_type)
            if previous is None or reason.fired_at_unix_ms >= previous.fired_at_unix_ms:
                latest[reason.trigger_type] = reason

        return [
            PreArmTriggerReason(
                trigger_type=item.trigger_type,
                severity=item.severity,
                observed_value=item.observed_value,
                threshold_value=item.threshold_value,
                observed_label=item.observed_label,
                threshold_label=item.threshold_label,
                fired_at_unix_ms=item.fired_at_unix_ms,
                summary=item.summary,
                details=dict(item.details),
            )
            for item in latest.values()
        ]

    @staticmethod
    def _window_to_dict(window: PreArmWindow | None) -> dict[str, object] | None:
        if window is None:
            return None
        return asdict(window)

    @staticmethod
    def _detail_has_content(detail: CeDetail) -> bool:
        for value in asdict(detail).values():
            if value is None:
                continue
            if isinstance(value, dict) and not value:
                continue
            if isinstance(value, str) and not value:
                continue
            return True
        return False

    def _normalize_payload_snippet(self, raw: str) -> str | None:
        max_bytes = self._detail_max_payload_bytes
        if max_bytes <= 0:
            return None

        redacted = self._redact_json_string(raw)
        encoded = redacted.encode("utf-8")
        if len(encoded) <= max_bytes:
            return redacted

        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def _redact_json_string(self, raw: str) -> str:
        if not self._redact_fields:
            return raw

        try:
            parsed = json.loads(raw)
        except Exception:
            return raw

        def scrub(value: object) -> object:
            if isinstance(value, dict):
                out: dict[str, object] = {}
                for key, item in value.items():
                    if key.lower() in self._redact_fields:
                        out[key] = "<redacted>"
                    else:
                        out[key] = scrub(item)
                return out
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        try:
            return json.dumps(scrub(parsed), separators=(",", ":"))
        except Exception:
            return raw

    @staticmethod
    def _now_clock() -> _Clock:
        wall_ms = int(time.time() * 1000)
        mono_ms = int(time.monotonic_ns() / 1_000_000)
        return _Clock(
            wall_ms=wall_ms,
            wall_sec=wall_ms // 1000,
            mono_ms=mono_ms,
        )
