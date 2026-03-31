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
        ce_id=str(uuid.uuid4()),
        trace_id=str(uuid.uuid4()),
        parent_ce_id=None,
        service_id="svc",
        wall_ts_ns=int(time.time() * 1_000_000_000),
        kind="HTTP_IN",
        status=200,
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
    assert (
        debug["active_prearm_window"]["reasons"][0]["trigger_type"] == "error_rate_5xx"
    )


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

    assert wait_until(
        lambda: client.get_capture_mode() == CaptureMode.NORMAL, timeout_s=0.35
    )

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
        RecordEventOptions(
            status=202, event_attrs={"endpoint": "payments"}, wall_ts_ns=now_ns + 5
        )
    )

    events = client._buffer.flush(window_ms=60_000)  # type: ignore[attr-defined]
    by_type = {event.event_type: event for event in events}

    assert by_type["queue_publish"].kind == "QUEUE_PUBLISH"
    assert by_type["queue_publish"].status == 0
    assert by_type["queue_publish"].event_class == "causal"

    assert by_type["queue_consume"].kind == "QUEUE_CONSUME"
    assert by_type["queue_consume"].status == 0
    assert by_type["queue_consume"].event_class == "causal"

    assert by_type["job_start"].kind == "INTERNAL"
    assert by_type["job_start"].status == 0
    assert by_type["job_start"].event_class == "causal"

    assert by_type["job_end"].kind == "INTERNAL"
    assert by_type["job_end"].status == 0
    assert by_type["job_end"].event_class == "causal"

    assert by_type["webhook_in"].kind == "HTTP_IN"
    assert by_type["webhook_in"].status == 200
    assert by_type["webhook_in"].event_class == "causal"

    assert by_type["webhook_out"].kind == "HTTP_OUT"
    assert by_type["webhook_out"].status == 202
    assert by_type["webhook_out"].event_attrs == {"endpoint": "payments"}


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
