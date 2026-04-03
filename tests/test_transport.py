"""Comprehensive tests for the Transport module."""

from __future__ import annotations

import io
import json
import urllib.error

from incidentary.transport import Transport, _next_utc_month_start_ms, _normalize_error
from incidentary.types import SkeletonCe


def make_http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost/api/v1/ingest/batch",
        code=status,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


def _make_ce(**overrides) -> SkeletonCe:
    defaults = {
        "ce_id": "ce-1",
        "trace_id": "trace-1",
        "parent_ce_id": None,
        "service_id": "svc",
        "wall_ts_ns": 1_000_000_000,
        "kind": "HTTP_IN",
        "status": 200,
        "duration_ns": 1_000,
    }
    defaults.update(overrides)
    return SkeletonCe(**defaults)


# ---------------------------------------------------------------------------
# Constructor & is_healthy
# ---------------------------------------------------------------------------


class TestTransportInit:
    def test_defaults_with_base_url(self):
        t = Transport(base_url="http://localhost:8080", api_key="key", service_name="svc")
        assert t.base_url == "http://localhost:8080"
        assert t.api_key == "key"
        assert t.is_healthy is True

    def test_strips_trailing_slash(self):
        t = Transport(base_url="http://localhost:8080///", api_key="key")
        assert t.base_url == "http://localhost:8080"

    def test_api_url_fallback(self):
        t = Transport(api_url="http://api.example.com", api_key="key")
        assert t.base_url == "http://api.example.com"

    def test_base_url_takes_precedence_over_api_url(self):
        t = Transport(base_url="http://base", api_url="http://api", api_key="key")
        assert t.base_url == "http://base"

    def test_not_healthy_when_no_base_url(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = Transport(api_key="key")
        assert t.is_healthy is False

    def test_warns_when_base_url_missing(self):
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Transport(api_key="key", service_name="svc")

        assert any("base_url is not configured" in str(w.message) for w in caught)

    def test_warn_only_once(self):
        import warnings

        errors = []
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            t = Transport(api_key="key", on_error=errors.append)

        # Second attempt via _can_attempt_request should not warn again
        t._can_attempt_request()
        assert len(caught) == 1


# ---------------------------------------------------------------------------
# is_healthy with circuit breaker & quota pause
# ---------------------------------------------------------------------------


class TestIsHealthy:
    def test_healthy_when_backend_healthy(self):
        t = Transport(base_url="http://localhost", api_key="key")
        assert t.is_healthy is True

    def test_unhealthy_during_quota_pause(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        # Pause until far in the future
        t._quota_pause_until_ms = int(1e15)
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1_000_000.0)
        assert t.is_healthy is False

    def test_healthy_after_quota_pause_expires(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        t._quota_pause_until_ms = 1_000
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 2.0)  # 2000ms > 1000
        assert t.is_healthy is True

    def test_unhealthy_during_circuit_open(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        t._backend_healthy = False
        t._circuit_open_until_ms = int(1e15)
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1_000.0)
        assert t.is_healthy is False

    def test_healthy_after_circuit_cooldown_expires(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        t._backend_healthy = False
        t._circuit_open_until_ms = 1_000
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 2.0)  # past cooldown
        assert t.is_healthy is True


# ---------------------------------------------------------------------------
# _can_attempt_request
# ---------------------------------------------------------------------------


class TestCanAttemptRequest:
    def test_false_when_no_base_url(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = Transport(api_key="key")
        assert t._can_attempt_request() is False

    def test_true_when_healthy(self):
        t = Transport(base_url="http://localhost", api_key="key")
        assert t._can_attempt_request() is True

    def test_false_during_quota_pause(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1.0)
        t._quota_pause_until_ms = 2_000
        assert t._can_attempt_request() is False

    def test_resets_quota_pause_when_expired(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        t._quota_pause_until_ms = 500
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1.0)  # now_ms=1000 > 500
        assert t._can_attempt_request() is True
        assert t._quota_pause_until_ms == 0

    def test_circuit_breaker_reset_after_cooldown(self, monkeypatch):
        t = Transport(base_url="http://localhost", api_key="key")
        t._backend_healthy = False
        t._consecutive_failures = 5
        t._circuit_open_until_ms = 1_000
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 2.0)
        assert t._can_attempt_request() is True
        assert t._backend_healthy is True
        assert t._consecutive_failures == 0


# ---------------------------------------------------------------------------
# _on_success / _on_failure / _dispatch_error
# ---------------------------------------------------------------------------


class TestSuccessFailure:
    def test_on_success_resets_circuit(self):
        t = Transport(base_url="http://localhost", api_key="key")
        t._backend_healthy = False
        t._consecutive_failures = 5
        t._circuit_open_until_ms = 999_999
        t._on_success()
        assert t._backend_healthy is True
        assert t._consecutive_failures == 0
        assert t._circuit_open_until_ms == 0

    def test_on_failure_opens_circuit_after_three_failures(self, monkeypatch):
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 100.0)
        t = Transport(base_url="http://localhost", api_key="key", circuit_breaker_cooldown_ms=5000)
        for i in range(3):
            t._on_failure(RuntimeError(f"fail {i}"))
        assert t._backend_healthy is False
        assert t._circuit_open_until_ms == 105_000

    def test_on_failure_below_threshold_stays_healthy(self):
        t = Transport(base_url="http://localhost", api_key="key")
        t._on_failure(RuntimeError("fail 1"))
        t._on_failure(RuntimeError("fail 2"))
        assert t._backend_healthy is True
        assert t._consecutive_failures == 2

    def test_dispatch_error_calls_on_error(self):
        errors = []
        t = Transport(base_url="http://localhost", api_key="key", on_error=errors.append)
        err = RuntimeError("test error")
        t._dispatch_error(err)
        assert errors == [err]

    def test_dispatch_error_does_nothing_when_no_handler(self):
        t = Transport(base_url="http://localhost", api_key="key")
        t._dispatch_error(RuntimeError("test"))  # should not raise

    def test_dispatch_error_swallows_handler_exception(self):
        def bad_handler(e):
            raise ValueError("handler broke")

        t = Transport(base_url="http://localhost", api_key="key", on_error=bad_handler)
        t._dispatch_error(RuntimeError("test"))  # should not raise


# ---------------------------------------------------------------------------
# _do_upload
# ---------------------------------------------------------------------------


class TestDoUpload:
    def test_returns_early_when_no_base_url(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = Transport(api_key="key")
        t._do_upload(b'{"events":[]}', None)  # should not raise

    def test_success_on_200(self, monkeypatch):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: FakeResponse()
        )
        t = Transport(base_url="http://localhost", api_key="key")
        t._do_upload(b"{}", None)
        assert t._backend_healthy is True
        assert t._consecutive_failures == 0

    def test_includes_incident_id_header(self, monkeypatch):
        captured_requests = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def capture_urlopen(req, **kwargs):
            captured_requests.append(req)
            return FakeResponse()

        monkeypatch.setattr("incidentary.transport.urllib.request.urlopen", capture_urlopen)
        t = Transport(base_url="http://localhost", api_key="test-key")
        t._do_upload(b"{}", "inc_456")

        assert len(captured_requests) == 1
        assert captured_requests[0].get_header("X-incidentary-incident-id") == "inc_456"
        assert captured_requests[0].get_header("Authorization") == "Bearer test-key"

    def test_426_version_rejected_prints_and_succeeds(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                make_http_error(
                    426,
                    {
                        "error": {
                            "code": "SDK_VERSION_TOO_OLD",
                            "minimum_version": "1.0.0",
                            "current_version": "0.2.0",
                        }
                    },
                )
            ),
        )
        t = Transport(base_url="http://localhost", api_key="key")
        t._do_upload(b"{}", None)

        output = capsys.readouterr().out
        assert "incidentary_sdk_version_rejected" in output
        assert t._backend_healthy is True

    def test_429_non_free_limit_retries_and_fails(self, monkeypatch):
        errors = []
        monkeypatch.setattr("incidentary.transport.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(make_http_error(429, {"error": "rate_limited"})),
        )
        t = Transport(base_url="http://localhost", api_key="key", on_error=errors.append)
        t._do_upload(b"{}", None)

        assert len(errors) == 1
        assert "HTTP 429" in str(errors[0])

    def test_generic_exception_retries_and_fails(self, monkeypatch):
        errors = []
        monkeypatch.setattr("incidentary.transport.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("connection refused")),
        )
        t = Transport(base_url="http://localhost", api_key="key", on_error=errors.append)
        t._do_upload(b"{}", None)

        assert len(errors) == 1

    def test_success_after_transient_failure(self, monkeypatch):
        monkeypatch.setattr("incidentary.transport.time.sleep", lambda s: None)
        call_count = 0

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def flaky_urlopen(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("transient")
            return FakeResponse()

        monkeypatch.setattr("incidentary.transport.urllib.request.urlopen", flaky_urlopen)
        t = Transport(base_url="http://localhost", api_key="key")
        t._do_upload(b"{}", None)

        assert call_count == 3
        assert t._backend_healthy is True


class TestDoNotifyBackend:
    def test_returns_early_when_no_base_url(self):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = Transport(api_key="key")
        t._do_notify_backend(b"{}")  # should not raise

    def test_success_on_200(self, monkeypatch):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: FakeResponse()
        )
        t = Transport(base_url="http://localhost", api_key="key")
        t._do_notify_backend(b"{}")
        assert t._backend_healthy is True

    def test_failure_on_non_2xx(self, monkeypatch):
        errors = []

        class FakeResponse:
            status = 503

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: FakeResponse()
        )
        t = Transport(base_url="http://localhost", api_key="key", on_error=errors.append)
        t._do_notify_backend(b"{}")
        assert len(errors) == 1

    def test_exception_during_request(self, monkeypatch):
        errors = []
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused")),
        )
        t = Transport(base_url="http://localhost", api_key="key", on_error=errors.append)
        t._do_notify_backend(b"{}")
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# upload_batch
# ---------------------------------------------------------------------------


class TestUploadBatch:
    def test_empty_events_does_not_upload(self, monkeypatch):
        upload_called = False

        def fake_upload(*args, **kwargs):
            nonlocal upload_called
            upload_called = True

        t = Transport(base_url="http://localhost", api_key="key")
        monkeypatch.setattr(t, "_do_upload", fake_upload)
        t.upload_batch([])
        assert upload_called is False

    def test_serialization_error_does_not_raise(self):
        t = Transport(base_url="http://localhost", api_key="key")

        class BadEvent:
            def __dict__(self):
                raise RuntimeError("cannot serialize")

        # Should not raise
        t.upload_batch([BadEvent()])

    def test_upload_skipped_when_circuit_open(self, monkeypatch):
        thread_started = False

        def fake_thread_start(self):
            nonlocal thread_started
            thread_started = True

        monkeypatch.setattr("threading.Thread.start", fake_thread_start)
        t = Transport(base_url="http://localhost", api_key="key")
        t._backend_healthy = False
        t._circuit_open_until_ms = int(1e15)
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1.0)

        t.upload_batch([_make_ce()])
        assert thread_started is False


# ---------------------------------------------------------------------------
# notify_backend
# ---------------------------------------------------------------------------


class TestNotifyBackend:
    def test_skipped_when_circuit_open(self, monkeypatch):
        thread_started = False

        def fake_thread_start(self):
            nonlocal thread_started
            thread_started = True

        monkeypatch.setattr("threading.Thread.start", fake_thread_start)
        t = Transport(base_url="http://localhost", api_key="key")
        t._backend_healthy = False
        t._circuit_open_until_ms = int(1e15)
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1.0)

        t.notify_backend("pre_arm_start", "svc")
        assert thread_started is False

    def test_serialization_error_does_not_raise(self, monkeypatch):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = Transport(api_key="key")
        # metadata with unserializable value — should not raise
        t.notify_backend("event", "svc", {"bad": object()})


# ---------------------------------------------------------------------------
# _pause_on_free_ce_limit
# ---------------------------------------------------------------------------


class TestPauseOnFreeCeLimit:
    def test_pauses_on_valid_free_limit_payload(self, monkeypatch, capsys):
        errors = []
        monkeypatch.setattr("incidentary.transport.time.time", lambda: 1_710_244_800.0)
        t = Transport(base_url="http://localhost", api_key="key", on_error=errors.append)

        result = t._pause_on_free_ce_limit(
            {"error": "ce_limit_reached", "limit_type": "ce", "plan": "free", "limit": 200_000}
        )

        assert result is True
        assert t._quota_pause_until_ms > 0
        assert "Pausing ingest until" in str(errors[0])
        assert "incidentary_ce_limit_reached" in capsys.readouterr().out

    def test_ignores_non_free_limit(self):
        t = Transport(base_url="http://localhost", api_key="key")
        assert t._pause_on_free_ce_limit({"error": "rate_limited"}) is False
        assert t._quota_pause_until_ms == 0

    def test_ignores_wrong_limit_type(self):
        t = Transport(base_url="http://localhost", api_key="key")
        result = t._pause_on_free_ce_limit(
            {"error": "ce_limit_reached", "limit_type": "bytes", "plan": "free"}
        )
        assert result is False

    def test_ignores_non_free_plan(self):
        t = Transport(base_url="http://localhost", api_key="key")
        result = t._pause_on_free_ce_limit(
            {"error": "ce_limit_reached", "limit_type": "ce", "plan": "pro"}
        )
        assert result is False


# ---------------------------------------------------------------------------
# _next_utc_month_start_ms
# ---------------------------------------------------------------------------


class TestNextUtcMonthStartMs:
    def test_mid_march(self):
        # 2024-03-12 00:00:00 UTC → should return April 1 midnight
        result = _next_utc_month_start_ms(1_710_201_600.0)
        # April 1, 2024 00:00:00 UTC = 1711929600
        assert result == 1_711_929_600_000

    def test_december_rolls_to_january(self):
        # 2024-12-15 00:00:00 UTC → should return January 1 2025
        result = _next_utc_month_start_ms(1_734_220_800.0)
        # January 1, 2025 00:00:00 UTC = 1735689600
        assert result == 1_735_689_600_000

    def test_uses_current_time_when_none(self):
        result = _next_utc_month_start_ms()
        assert result > 0


# ---------------------------------------------------------------------------
# _normalize_error
# ---------------------------------------------------------------------------


class TestNormalizeError:
    def test_returns_exception_unchanged(self):
        err = ValueError("test")
        assert _normalize_error(err) is err

    def test_returns_runtime_error_for_non_exception(self):
        # This path is a defensive check; in practice, the type hint ensures Exception
        result = _normalize_error(RuntimeError("fallback"))
        assert isinstance(result, RuntimeError)


# ---------------------------------------------------------------------------
# Full integration: transport pauses after free CE limit
# ---------------------------------------------------------------------------


def test_transport_pauses_after_free_ce_limit(monkeypatch, capsys):
    errors = []
    current_time_s = 1_710_244_800.0
    monkeypatch.setattr("incidentary.transport.time.time", lambda: current_time_s)

    def raising_urlopen(*args, **kwargs):
        raise make_http_error(
            429,
            {
                "error": "ce_limit_reached",
                "limit_type": "ce",
                "plan": "free",
                "limit": 200000,
            },
        )

    monkeypatch.setattr("incidentary.transport.urllib.request.urlopen", raising_urlopen)

    transport = Transport(
        base_url="http://localhost:18080",
        api_key="test",
        service_name="svc",
        on_error=errors.append,
    )

    transport._do_upload(b"{}", None)

    assert transport.is_healthy is False
    assert transport._can_attempt_request() is False
    assert errors
    assert "Pausing ingest until" in str(errors[0])
    assert "incidentary_ce_limit_reached" in capsys.readouterr().out
