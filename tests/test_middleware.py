import uuid

import pytest

from incidentary.client import IncidentaryClient
from incidentary.middleware import (
    IncidentaryWSGIMiddleware,
    extract_trace_context,
    inject_trace_context,
    instrumented_urlopen,
)
from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER


def make_client() -> IncidentaryClient:
    return IncidentaryClient(
        api_key="test",
        service_name="svc",
        base_url="http://localhost:18080",
        pre_arm_enable_slow_success=False,
        pre_arm_enable_inflight=False,
        pre_arm_enable_retry=False,
    )


def test_extract_trace_context_propagates_existing_header():
    trace = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    trace_id, parent_id = extract_trace_context({TRACE_ID_HEADER: trace, PARENT_CE_HEADER: parent})
    assert trace_id == trace
    assert parent_id == parent


def test_extract_trace_context_generates_when_missing():
    trace_id, parent_id = extract_trace_context({})
    assert isinstance(trace_id, str)
    assert parent_id is None


def test_inject_trace_context_sets_headers():
    headers = {}
    inject_trace_context(headers, "trace-1", "ce-1")
    assert headers[TRACE_ID_HEADER] == "trace-1"
    assert headers[PARENT_CE_HEADER] == "ce-1"


def test_wsgi_middleware_captures_request_without_throwing():
    client = make_client()

    def app(environ, start_response):
        start_response("200 OK", [])
        return [b"ok"]

    middleware = IncidentaryWSGIMiddleware(app, client)

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/",
        "HTTP_X_INCIDENTARY_TRACE_ID": str(uuid.uuid4()),
    }

    def start_response(status, headers, exc_info=None):
        return None

    result = middleware(environ, start_response)
    assert result == [b"ok"]


class _StubClient:
    def __init__(self):
        self.service_name = "svc"
        self.start_kinds = []
        self.request_calls = []
        self.events = []

    def record_request_start(self, kind="HTTP_IN"):
        self.start_kinds.append(kind)

    def record_request(self, status_code, options=None):
        self.request_calls.append((status_code, options))

    def should_capture_detail_for_current_mode(self):
        return True

    def get_detail_request_header_allowlist(self):
        return ["content-type", "content-length"]

    def get_detail_response_header_allowlist(self):
        return ["content-type", "content-length"]

    def attach_detail_to_event(self, ce, detail):
        ce.detail = detail
        return ce

    def write_event(self, ce):
        self.events.append(ce)


def test_instrumented_urlopen_records_retry_key_quality(monkeypatch):
    class _Response:
        status = 200
        headers = {"content-type": "application/json", "content-length": "2"}

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: _Response())

    client = _StubClient()

    instrumented_urlopen(
        client,
        {"trace_id": "trace-1", "ce_id": "ce-parent"},
        "https://billing.internal/charges/123/capture?expand=true",
        method="POST",
        headers={"content-type": "application/json"},
        data=b"{}",
        retry_metadata={
            "route_template": "/charges/:id/capture",
            "downstream_service": "billing",
            "retry_attempt": 2,
        },
    )

    assert client.start_kinds == ["HTTP_OUT"]
    assert len(client.request_calls) == 1
    status_code, options = client.request_calls[0]
    assert status_code == 200
    assert options.kind == "HTTP_OUT"
    assert options.outbound_retry_key_quality == "route_template"
    assert options.explicit_retry_observed is True
    assert len(client.events) == 1
    assert client.events[0].detail is not None


# ---------------------------------------------------------------------------
# ASGI Middleware
# ---------------------------------------------------------------------------


class TestASGIMiddleware:
    async def test_asgi_middleware_captures_http_request(self):
        from incidentary.middleware import IncidentaryASGIMiddleware

        client = _StubClient()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = IncidentaryASGIMiddleware(app, client)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
        }

        sent_messages = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            sent_messages.append(message)

        await middleware(scope, receive, send)

        assert len(sent_messages) == 2
        assert client.start_kinds == ["HTTP_IN"]
        assert len(client.request_calls) == 1
        assert client.request_calls[0][0] == 200

    async def test_asgi_middleware_passes_through_non_http(self):
        from incidentary.middleware import IncidentaryASGIMiddleware

        client = _StubClient()
        called = False

        async def app(scope, receive, send):
            nonlocal called
            called = True

        middleware = IncidentaryASGIMiddleware(app, client)
        scope = {"type": "lifespan"}

        await middleware(scope, None, None)
        assert called is True
        assert client.start_kinds == []

    async def test_asgi_middleware_extracts_trace_headers(self):
        from incidentary.middleware import IncidentaryASGIMiddleware

        client = _StubClient()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 201})
            await send({"type": "http.response.body", "body": b""})

        middleware = IncidentaryASGIMiddleware(app, client)
        scope = {
            "type": "http",
            "headers": [
                (b"x-incidentary-trace-id", b"trace-asgi"),
                (b"x-incidentary-parent-ce", b"parent-asgi"),
            ],
        }

        async def async_receive():
            return {"type": "http.request", "body": b""}

        async def async_send(message):
            pass

        await middleware(scope, async_receive, async_send)

        assert len(client.events) == 1
        assert client.events[0].trace_id == "trace-asgi"
        assert client.events[0].parent_id == "parent-asgi"
        assert client.request_calls[0][0] == 201

    async def test_asgi_middleware_default_status_when_no_response_start(self):
        from incidentary.middleware import IncidentaryASGIMiddleware

        client = _StubClient()

        async def app(scope, receive, send):
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = IncidentaryASGIMiddleware(app, client)
        scope = {"type": "http", "headers": []}

        async def async_receive():
            return {"type": "http.request", "body": b""}

        async def async_send(message):
            pass

        await middleware(scope, async_receive, async_send)

        # Default status should be 200
        assert client.request_calls[0][0] == 200


# ---------------------------------------------------------------------------
# instrumented_urlopen error paths
# ---------------------------------------------------------------------------


class TestInstrumentedUrlopenErrors:
    def test_http_error_records_status_and_reraises(self, monkeypatch):
        import urllib.error

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(
                urllib.error.HTTPError(
                    url="http://example.com",
                    code=502,
                    msg="Bad Gateway",
                    hdrs=None,
                    fp=__import__("io").BytesIO(b"error"),
                )
            ),
        )
        client = _StubClient()

        with pytest.raises(urllib.error.HTTPError):
            instrumented_urlopen(client, None, "http://example.com/test")

        assert client.request_calls[0][0] == 502
        assert client.events[0].status_code == 502

    def test_timeout_error_sets_timed_out_flag(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")),
        )
        client = _StubClient()

        with pytest.raises(TimeoutError):
            instrumented_urlopen(client, None, "http://example.com/test")

        assert client.request_calls[0][0] == 0
        assert client.request_calls[0][1].timed_out is True
        assert client.request_calls[0][1].cancelled is True

    def test_generic_error_records_zero_status(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused")),
        )
        client = _StubClient()

        with pytest.raises(ConnectionError):
            instrumented_urlopen(client, None, "http://example.com/test")

        assert client.request_calls[0][0] == 0
        assert client.request_calls[0][1].timed_out is False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_extract_trace_context_ignores_non_string_values(self):
        """Non-string header values should be ignored."""
        trace_id, parent = extract_trace_context(
            {TRACE_ID_HEADER: 12345, PARENT_CE_HEADER: None}  # type: ignore
        )
        assert isinstance(trace_id, str)
        assert parent is None

    def test_parse_status_code_valid(self):
        from incidentary.middleware import _parse_status_code

        assert _parse_status_code("200 OK") == 200
        assert _parse_status_code("404 Not Found") == 404
        assert _parse_status_code("500 Internal Server Error") == 500

    def test_parse_status_code_invalid(self):
        from incidentary.middleware import _parse_status_code

        assert _parse_status_code("") == 0
        assert _parse_status_code("abc") == 0

    def test_parse_content_length(self):
        from incidentary.middleware import _parse_content_length

        assert _parse_content_length(None) is None
        assert _parse_content_length("42") == 42
        assert _parse_content_length("0") == 0
        assert _parse_content_length("-1") is None
        assert _parse_content_length("abc") is None

    def test_optional_str(self):
        from incidentary.middleware import _optional_str

        assert _optional_str("hello") == "hello"
        assert _optional_str("  spaces  ") == "spaces"
        assert _optional_str("") is None
        assert _optional_str("   ") is None
        assert _optional_str(None) is None
        assert _optional_str(123) is None

    def test_filter_headers(self):
        from incidentary.middleware import _filter_headers

        headers = {"Content-Type": "text/html", "X-Custom": "val", "Authorization": "Bearer x"}
        result = _filter_headers(headers, ["content-type", "x-custom"])
        assert result == {"content-type": "text/html", "x-custom": "val"}

    def test_filter_headers_empty_allowlist(self):
        from incidentary.middleware import _filter_headers

        assert _filter_headers({"a": "b"}, []) is None

    def test_filter_headers_no_match(self):
        from incidentary.middleware import _filter_headers

        assert _filter_headers({"a": "b"}, ["x-nonexistent"]) is None

    def test_hash_retry_identity_deterministic(self):
        from incidentary.middleware import _hash_retry_identity

        h1 = _hash_retry_identity("GET:/orders/:id")
        h2 = _hash_retry_identity("GET:/orders/:id")
        assert h1 == h2
        assert isinstance(h1, int)

    def test_hash_retry_identity_different_inputs(self):
        from incidentary.middleware import _hash_retry_identity

        h1 = _hash_retry_identity("GET:/orders/:id")
        h2 = _hash_retry_identity("POST:/orders/:id")
        assert h1 != h2

    def test_extract_explicit_retry_observed(self):
        from incidentary.middleware import _extract_explicit_retry_observed

        assert _extract_explicit_retry_observed(None) is None
        assert _extract_explicit_retry_observed({"retry_attempt": 1}) is False
        assert _extract_explicit_retry_observed({"retry_attempt": 2}) is True
        assert _extract_explicit_retry_observed({"retry_attempt": 3}) is True
        assert _extract_explicit_retry_observed({"is_retry": True}) is True
        assert _extract_explicit_retry_observed({"is_retry": False}) is False
        assert _extract_explicit_retry_observed({"other": "value"}) is None


# ---------------------------------------------------------------------------
# _build_inbound_detail
# ---------------------------------------------------------------------------


class TestBuildInboundDetail:
    def test_returns_none_when_not_capturing(self):
        from incidentary.middleware import _build_inbound_detail

        client = _StubClient()
        client.should_capture_detail_for_current_mode = lambda: False
        assert _build_inbound_detail(client, {}, {}, []) is None

    def test_builds_detail_with_environ(self):
        from incidentary.middleware import _build_inbound_detail

        client = _StubClient()
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/orders/123",
            "CONTENT_LENGTH": "42",
        }
        request_headers = {"content-type": "application/json"}
        response_headers = [("content-type", "application/json"), ("content-length", "10")]

        detail = _build_inbound_detail(client, environ, request_headers, response_headers)
        assert detail is not None
        assert detail.method == "POST"
        assert detail.request_bytes == 42
        assert detail.response_bytes == 10


# ---------------------------------------------------------------------------
# _build_outbound_detail
# ---------------------------------------------------------------------------


class TestBuildOutboundDetail:
    def test_returns_none_when_not_capturing(self):
        from incidentary.downstream_edge_key import DownstreamEdgeKeyResolution
        from incidentary.middleware import _build_outbound_detail

        client = _StubClient()
        client.should_capture_detail_for_current_mode = lambda: False
        edge = DownstreamEdgeKeyResolution(
            route_key="GET:/test",
            edge_key="test",
            operation_key="test",
            key_quality="unknown",
            key_for_hash="test",
        )
        assert (
            _build_outbound_detail(client, {}, {}, "GET", edge, None, None, None, False, False)
            is None
        )

    def test_builds_detail_with_request_body(self):
        from incidentary.downstream_edge_key import DownstreamEdgeKeyResolution
        from incidentary.middleware import _build_outbound_detail

        client = _StubClient()
        edge = DownstreamEdgeKeyResolution(
            route_key="POST:/charges",
            edge_key="charges",
            operation_key="charges",
            key_quality="route_template",
            key_for_hash="POST:/charges",
        )
        detail = _build_outbound_detail(
            client,
            {"content-type": "application/json"},
            {"content-length": "5"},
            "POST",
            edge,
            {
                "route_template": "/charges/:id",
                "downstream_service": "billing",
                "operation_name": "capture",
            },
            True,
            b"hello",
            False,
            False,
        )
        assert detail is not None
        assert detail.method == "POST"
        assert detail.request_bytes == 5  # len(b'hello')
        assert detail.response_bytes == 5
        assert detail.payload_snippet == "hello"
        assert detail.retry["explicit_observed"] is True
        assert detail.downstream["service"] == "billing"

    def test_timeout_classification(self):
        from incidentary.downstream_edge_key import DownstreamEdgeKeyResolution
        from incidentary.middleware import _build_outbound_detail

        client = _StubClient()
        edge = DownstreamEdgeKeyResolution(
            route_key="GET:/test",
            edge_key="test",
            operation_key="test",
            key_quality="unknown",
            key_for_hash="test",
        )
        detail = _build_outbound_detail(
            client,
            {},
            {},
            "GET",
            edge,
            None,
            None,
            None,
            False,
            True,
        )
        assert detail.local_error_classification == "timeout"

    def test_cancelled_classification(self):
        from incidentary.downstream_edge_key import DownstreamEdgeKeyResolution
        from incidentary.middleware import _build_outbound_detail

        client = _StubClient()
        edge = DownstreamEdgeKeyResolution(
            route_key="GET:/test",
            edge_key="test",
            operation_key="test",
            key_quality="unknown",
            key_for_hash="test",
        )
        detail = _build_outbound_detail(
            client,
            {},
            {},
            "GET",
            edge,
            None,
            None,
            None,
            True,
            False,
        )
        assert detail.local_error_classification == "cancelled"
