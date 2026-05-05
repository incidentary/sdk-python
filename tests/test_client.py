import threading
import time
import uuid
import warnings

from incidentary.client import IncidentaryClient, _ShardedCounter
from incidentary.types import CaptureMode, CeDetail, RecordEventOptions, SkeletonCe


def make_client(**overrides):
    config = {
        "api_key": "test",
        "service_name": "svc",
        "base_url": "http://localhost:18080",
        "pre_arm_threshold_high": 10,
        "pre_arm_threshold_low": 2,
        "pre_arm_min_duration_ms": 0,
        "pre_arm_ttl_ms": 500,
        "pre_arm_cooldown_ms": 0,
        "pre_arm_enable_slow_success": False,
        "pre_arm_enable_inflight": False,
        "pre_arm_enable_retry": False,
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


def wait_until(predicate, timeout_s: float = 0.5) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_initial_mode_normal():
    assert make_client().get_capture_mode() == CaptureMode.NORMAL


def test_enters_pre_armed_on_error_spike_with_reason_metadata():
    client = make_client()
    for _ in range(8):
        client.record_request(200)
    for _ in range(2):
        client.record_request(500)

    assert client.get_capture_mode() == CaptureMode.PRE_ARMED

    debug = client.get_prearm_debug_state()
    assert debug["active_prearm_window"] is not None
    assert debug["active_prearm_window"]["reasons"][0]["trigger_type"] == "error_rate_5xx"


def test_incident_transitions_preserve_bind_metadata():
    client = make_client()
    for _ in range(8):
        client.record_request(200)
    for _ in range(2):
        client.record_request(500)

    client.escalate_to_incident("inc_123")
    assert client.get_capture_mode() == CaptureMode.INCIDENT

    debug = client.get_prearm_debug_state()
    assert debug["active_prearm_window"]["bound_incident_id"] == "inc_123"
    assert debug["counters"]["prearm_bind_total"] == 1

    client.close_incident()
    assert client.get_capture_mode() == CaptureMode.NORMAL


def test_prearmed_expires_silently_without_bind():
    client = make_client(pre_arm_ttl_ms=40, pre_arm_min_duration_ms=0)

    for _ in range(8):
        client.record_request(200)
    for _ in range(2):
        client.record_request(500)
    assert client.get_capture_mode() == CaptureMode.PRE_ARMED

    assert wait_until(lambda: client.get_capture_mode() == CaptureMode.NORMAL, timeout_s=0.35)

    debug = client.get_prearm_debug_state()
    assert debug["counters"]["prearm_expire_total"] >= 1
    assert len(debug["recent_prearm_windows"]) >= 1


def test_detail_capture_changes_between_normal_and_prearmed():
    client = make_client(
        pre_arm_detail_capture_payload_enabled=True,
        pre_arm_detail_max_payload_bytes=64,
    )

    ce = make_ce()
    normal = client.attach_detail_to_event(
        ce,
        CeDetail(
            method="GET",
            route_key="/orders/:id",
            payload_snippet='{"password":"secret","ok":"x"}',
        ),
    )
    assert normal.detail is None

    for _ in range(8):
        client.record_request(200)
    for _ in range(2):
        client.record_request(500)

    with_detail = client.attach_detail_to_event(
        make_ce(),
        CeDetail(
            method="POST",
            route_key="/charges/:id/capture",
            payload_snippet='{"password":"secret","token":"abc"}',
        ),
    )

    assert with_detail.detail is not None
    assert with_detail.detail.route_key == "/charges/:id/capture"
    assert with_detail.detail.payload_snippet is not None
    assert "<redacted>" in with_detail.detail.payload_snippet


def test_never_throws_for_bad_status_codes():
    client = make_client()
    for code in [-1, 0, 100, 200, 400, 500, 999]:
        client.record_request(code)


def test_warns_when_base_url_is_missing():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client = make_client(base_url=None, api_url=None)

    assert any("base_url is not configured" in str(item.message) for item in caught)
    client.flush_to_backend()


def test_write_event_never_throws():
    client = make_client()
    client.write_event(make_ce())


def test_record_event_wrappers_emit_queue_job_webhook_vocabulary():
    client = make_client()
    now_ns = int(time.time() * 1_000_000_000)

    client.record_queue_publish()
    client.record_queue_consume()
    client.record_job_start()
    client.record_job_end()
    client.record_webhook_in()
    client.record_webhook_out(
        RecordEventOptions(status=202, event_attrs={"endpoint": "payments"}, wall_ts_ns=now_ns + 5)
    )

    events = client._buffer.flush(window_ms=60_000)  # type: ignore[attr-defined]
    by_type = {event.event_type: event for event in events}

    assert by_type["queue_publish"].kind == "QUEUE_PUBLISH"
    assert by_type["queue_publish"].status_code == 0

    assert by_type["queue_consume"].kind == "QUEUE_CONSUME"
    assert by_type["queue_consume"].status_code == 0

    assert by_type["job_start"].kind == "INTERNAL"
    assert by_type["job_start"].status_code == 0

    assert by_type["job_end"].kind == "INTERNAL"
    assert by_type["job_end"].status_code == 0

    assert by_type["webhook_in"].kind == "HTTP_SERVER"
    assert by_type["webhook_in"].status_code == 200

    assert by_type["webhook_out"].kind == "HTTP_CLIENT"
    assert by_type["webhook_out"].status_code == 202
    assert by_type["webhook_out"].attributes == {"endpoint": "payments"}


def test_flush_annotates_prealert_events_with_ring_buffer_metadata():
    client = make_client()
    base_ms = int(time.time() * 1000)

    client.record_event(
        "http_in",
        RecordEventOptions(trace_id="trace-1", wall_ts_ns=(base_ms - 2) * 1_000_000),
    )
    for _ in range(8):
        client.record_request(200)
    for _ in range(2):
        client.record_request(500)
    client.record_event(
        "job_start",
        RecordEventOptions(trace_id="trace-1", wall_ts_ns=(base_ms - 1) * 1_000_000),
    )

    events = client._annotate_buffered_events_locked(client._buffer.flush())  # type: ignore[attr-defined]

    assert events[0].captured_before_alert is True
    assert events[0].ring_buffer_seq == 0
    assert events[1].captured_before_alert is True
    assert events[1].ring_buffer_seq == 1


def test_flush_keeps_postalert_events_distinct_after_bind():
    client = make_client()
    base_ms = int(time.time() * 1000)

    client.record_event(
        "http_in",
        RecordEventOptions(trace_id="trace-1", wall_ts_ns=(base_ms - 1) * 1_000_000),
    )
    for _ in range(8):
        client.record_request(200)
    for _ in range(2):
        client.record_request(500)

    client.escalate_to_incident("inc_123")
    client._pre_arm_alerted_at_ns = base_ms * 1_000_000  # type: ignore[attr-defined]
    client.record_event(
        "job_end",
        RecordEventOptions(trace_id="trace-1", wall_ts_ns=(base_ms + 1) * 1_000_000),
    )

    events = client._annotate_buffered_events_locked(client._buffer.flush())  # type: ignore[attr-defined]
    pre_alert, post_alert = events
    assert pre_alert.captured_before_alert is True
    assert pre_alert.ring_buffer_seq == 0
    assert post_alert.captured_before_alert is None
    assert post_alert.ring_buffer_seq is None


def test_sharded_counter_concurrent_add_and_drain():
    counter = _ShardedCounter(shards=16)
    writers = 8
    per_writer = 20_000
    expected_total = writers * per_writer
    drained_total = 0
    drained_lock = threading.Lock()
    done = False

    def worker():
        for _ in range(per_writer):
            counter.add(1)

    def drain_worker():
        nonlocal drained_total, done
        while not done:
            drained = counter.drain()
            if drained:
                with drained_lock:
                    drained_total += drained
            time.sleep(0)

    drain_thread = threading.Thread(target=drain_worker)
    drain_thread.start()

    threads = [threading.Thread(target=worker) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    done = True
    drain_thread.join()

    while True:
        drained = counter.drain()
        if drained == 0:
            break
        drained_total += drained

    assert drained_total == expected_total


# ---------------------------------------------------------------------------
# _normalize_request_options
# ---------------------------------------------------------------------------


class TestNormalizeRequestOptions:
    def test_none_returns_defaults(self):
        from incidentary.types import RecordRequestOptions

        result = IncidentaryClient._normalize_request_options(None)
        assert isinstance(result, RecordRequestOptions)
        assert result.kind == "HTTP_IN"
        assert result.duration_ns == 0

    def test_passthrough_record_request_options(self):
        from incidentary.types import RecordRequestOptions

        opts = RecordRequestOptions(kind="HTTP_OUT", duration_ns=42)
        result = IncidentaryClient._normalize_request_options(opts)
        assert result is opts

    def test_dict_with_snake_case(self):
        result = IncidentaryClient._normalize_request_options(
            {
                "kind": "HTTP_OUT",
                "duration_ns": 1000,
                "cancelled": True,
                "timed_out": True,
                "outbound_retry_key_hash": 12345,
                "outbound_retry_key_quality": "route_template",
                "explicit_retry_observed": True,
            }
        )
        assert result.kind == "HTTP_OUT"
        assert result.duration_ns == 1000
        assert result.cancelled is True
        assert result.timed_out is True
        assert result.outbound_retry_key_hash == 12345
        assert result.outbound_retry_key_quality == "route_template"
        assert result.explicit_retry_observed is True

    def test_dict_with_camel_case_fallbacks(self):
        result = IncidentaryClient._normalize_request_options(
            {
                "timedOut": True,
                "outboundRetryKeyHash": 99,
                "outboundRetryKeyQuality": "explicit",
                "explicitRetryObserved": False,
            }
        )
        assert result.timed_out is True
        assert result.outbound_retry_key_hash == 99
        assert result.outbound_retry_key_quality == "explicit"
        assert result.explicit_retry_observed is False

    def test_unsupported_type_returns_defaults(self):
        from incidentary.types import RecordRequestOptions

        result = IncidentaryClient._normalize_request_options("bad")
        assert isinstance(result, RecordRequestOptions)


# ---------------------------------------------------------------------------
# _detail_has_content
# ---------------------------------------------------------------------------


class TestDetailHasContent:
    def test_empty_detail_returns_false(self):
        detail = CeDetail()
        assert IncidentaryClient._detail_has_content(detail) is False

    def test_detail_with_method_returns_true(self):
        detail = CeDetail(method="GET")
        assert IncidentaryClient._detail_has_content(detail) is True

    def test_detail_with_empty_string_returns_false(self):
        detail = CeDetail(method="")
        assert IncidentaryClient._detail_has_content(detail) is False

    def test_detail_with_empty_dict_returns_false(self):
        detail = CeDetail(request_headers={})
        assert IncidentaryClient._detail_has_content(detail) is False

    def test_detail_with_populated_dict_returns_true(self):
        detail = CeDetail(request_headers={"content-type": "application/json"})
        assert IncidentaryClient._detail_has_content(detail) is True


# ---------------------------------------------------------------------------
# _normalize_payload_snippet / _redact_json_string
# ---------------------------------------------------------------------------


class TestPayloadRedaction:
    def test_truncation_to_max_bytes(self):
        client = make_client(
            pre_arm_detail_capture_payload_enabled=True,
            pre_arm_detail_max_payload_bytes=10,
        )
        result = client._normalize_payload_snippet("a" * 100)
        assert len(result.encode("utf-8")) <= 10

    def test_zero_max_bytes_returns_none(self):
        client = make_client(
            pre_arm_detail_capture_payload_enabled=True,
            pre_arm_detail_max_payload_bytes=0,
        )
        assert client._normalize_payload_snippet("anything") is None

    def test_short_payload_not_truncated(self):
        client = make_client(
            pre_arm_detail_capture_payload_enabled=True,
            pre_arm_detail_max_payload_bytes=1000,
        )
        result = client._normalize_payload_snippet("short")
        assert result == "short"

    def test_redact_json_replaces_sensitive_fields(self):
        client = make_client(redact_fields=["password", "token"])
        result = client._redact_json_string('{"password":"secret","name":"ok","token":"abc"}')
        parsed = __import__("json").loads(result)
        assert parsed["password"] == "<redacted>"
        assert parsed["token"] == "<redacted>"
        assert parsed["name"] == "ok"

    def test_redact_json_handles_nested(self):
        client = make_client(redact_fields=["secret"])
        result = client._redact_json_string('{"outer":{"secret":"val"},"items":[{"secret":"x"}]}')
        parsed = __import__("json").loads(result)
        assert parsed["outer"]["secret"] == "<redacted>"
        assert parsed["items"][0]["secret"] == "<redacted>"

    def test_redact_json_no_fields_passthrough(self):
        client = make_client(redact_fields=[])
        raw = '{"password":"secret"}'
        assert client._redact_json_string(raw) == raw

    def test_redact_json_invalid_json_passthrough(self):
        client = make_client(redact_fields=["password"])
        raw = "not json at all"
        assert client._redact_json_string(raw) == raw


# ---------------------------------------------------------------------------
# escalate_to_incident edge cases
# ---------------------------------------------------------------------------


class TestEscalateToIncident:
    def test_escalate_without_prior_prearm(self):
        client = make_client()
        client.escalate_to_incident("inc_1")
        assert client.get_capture_mode() == CaptureMode.INCIDENT

    def test_escalate_twice_preserves_incident(self):
        client = make_client()
        client.escalate_to_incident("inc_1")
        client.escalate_to_incident("inc_2")
        assert client.get_capture_mode() == CaptureMode.INCIDENT

    def test_close_incident_returns_to_normal(self):
        client = make_client()
        client.escalate_to_incident("inc_1")
        client.close_incident()
        assert client.get_capture_mode() == CaptureMode.NORMAL

    def test_close_incident_without_escalate(self):
        client = make_client()
        client.close_incident()  # should not raise
        assert client.get_capture_mode() == CaptureMode.NORMAL


# ---------------------------------------------------------------------------
# Event type mapping
# ---------------------------------------------------------------------------


class TestEventTypeMapping:
    def test_http_in_kind(self):
        client = make_client()
        assert client._event_type_to_kind("http_in") == "HTTP_SERVER"
        assert client._event_type_to_kind("http_server") == "HTTP_SERVER"
        assert client._event_type_to_kind("webhook_in") == "HTTP_SERVER"

    def test_http_out_kind(self):
        client = make_client()
        assert client._event_type_to_kind("http_out") == "HTTP_CLIENT"
        assert client._event_type_to_kind("http_client") == "HTTP_CLIENT"
        assert client._event_type_to_kind("webhook_out") == "HTTP_CLIENT"

    def test_queue_kinds(self):
        client = make_client()
        assert client._event_type_to_kind("queue_publish") == "QUEUE_PUBLISH"
        assert client._event_type_to_kind("queue_consume") == "QUEUE_CONSUME"

    def test_internal_kind_for_unknown(self):
        client = make_client()
        assert client._event_type_to_kind("job_start") == "INTERNAL"
        assert client._event_type_to_kind("custom_event") == "INTERNAL"

    def test_default_status_for_http_types(self):
        client = make_client()
        assert client._event_type_default_status("http_in") == 200
        assert client._event_type_default_status("webhook_out") == 200

    def test_default_status_for_non_http_types(self):
        client = make_client()
        assert client._event_type_default_status("queue_publish") == 0
        assert client._event_type_default_status("job_start") == 0


# ---------------------------------------------------------------------------
# record_event with various inputs
# ---------------------------------------------------------------------------


class TestRecordEvent:
    def test_record_event_with_defaults(self):
        client = make_client()
        client.record_event("http_in")
        events = client._buffer.flush()
        assert len(events) == 1
        assert events[0].event_type == "http_in"
        assert events[0].kind == "HTTP_SERVER"

    def test_record_event_with_options(self):
        client = make_client()
        client.record_event(
            "http_out",
            RecordEventOptions(
                trace_id="trace-1",
                parent_ce_id="parent-1",
                status=404,
                duration_ns=5000,
            ),
        )
        events = client._buffer.flush()
        assert len(events) == 1
        assert events[0].status_code == 404
        assert events[0].trace_id == "trace-1"
        assert events[0].duration_ns == 5000

    def test_record_event_negative_duration_clamped(self):
        client = make_client()
        client.record_event("http_in", RecordEventOptions(duration_ns=-100))
        events = client._buffer.flush()
        assert events[0].duration_ns == 0


# ---------------------------------------------------------------------------
# attach_detail_to_event edge cases
# ---------------------------------------------------------------------------


class TestAttachDetail:
    def test_no_detail_in_normal_mode(self):
        client = make_client()
        ce = make_ce()
        result = client.attach_detail_to_event(ce, CeDetail(method="GET", route_key="/test"))
        assert result.detail is None

    def test_none_detail_returns_ce_unchanged(self):
        client = make_client()
        # Force pre-armed mode
        for _ in range(8):
            client.record_request(200)
        for _ in range(2):
            client.record_request(500)
        ce = make_ce()
        result = client.attach_detail_to_event(ce, None)
        assert result.detail is None

    def test_payload_disabled_strips_snippet(self):
        client = make_client(
            pre_arm_detail_capture_payload_enabled=False,
        )
        # Enter pre-armed mode
        for _ in range(8):
            client.record_request(200)
        for _ in range(2):
            client.record_request(500)

        ce = make_ce()
        result = client.attach_detail_to_event(
            ce, CeDetail(method="POST", route_key="/api", payload_snippet='{"data":"val"}')
        )
        assert result.detail is not None
        assert result.detail.payload_snippet is None


# ---------------------------------------------------------------------------
# get_prearm_debug_state
# ---------------------------------------------------------------------------


class TestPreamDebugState:
    def test_debug_state_in_normal_mode(self):
        client = make_client()
        state = client.get_prearm_debug_state()
        assert state["gauges"]["current_prearm_state"] == "NORMAL"
        assert state["active_prearm_window"] is None

    def test_debug_state_in_prearmed_mode(self):
        client = make_client()
        for _ in range(8):
            client.record_request(200)
        for _ in range(2):
            client.record_request(500)

        state = client.get_prearm_debug_state()
        assert state["gauges"]["current_prearm_state"] == "PRE_ARMED"
        assert state["active_prearm_window"] is not None
        assert state["counters"]["prearm_enter_total"] >= 1


# ---------------------------------------------------------------------------
# Cooldown behavior
# ---------------------------------------------------------------------------


class TestCooldown:
    def test_cooldown_prevents_immediate_reenter(self):
        client = make_client(
            pre_arm_cooldown_ms=60_000, pre_arm_ttl_ms=50, pre_arm_min_duration_ms=0
        )
        # Enter pre-armed
        for _ in range(8):
            client.record_request(200)
        for _ in range(2):
            client.record_request(500)
        assert client.get_capture_mode() == CaptureMode.PRE_ARMED

        # Wait for TTL to expire
        assert wait_until(lambda: client.get_capture_mode() == CaptureMode.NORMAL, timeout_s=0.5)

        # Try to re-enter — cooldown should prevent it
        for _ in range(8):
            client.record_request(200)
        for _ in range(2):
            client.record_request(500)
        assert client.get_capture_mode() == CaptureMode.NORMAL
