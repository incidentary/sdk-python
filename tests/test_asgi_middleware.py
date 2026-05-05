"""Tests for ASGI middleware context propagation and request recording."""

import asyncio

from incidentary.context import clear_trace_context, get_trace_context
from incidentary.middleware import IncidentaryASGIMiddleware
from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER


class _StubClient:
    """Minimal stub implementing the client interface used by middleware."""

    def __init__(self):
        self.service_name = "svc"
        self.start_kinds: list[str] = []
        self.request_calls: list[tuple[int, object]] = []
        self.events: list[object] = []

    def record_request_start(self, kind="HTTP_IN"):
        self.start_kinds.append(kind)

    def record_request(self, status_code, options=None):
        self.request_calls.append((status_code, options))

    def should_capture_detail_for_current_mode(self):
        return False

    def get_detail_request_header_allowlist(self):
        return []

    def get_detail_response_header_allowlist(self):
        return []

    def attach_detail_to_event(self, ce, detail):
        return ce

    def write_event(self, ce):
        self.events.append(ce)


def _encode_header(name: str) -> bytes:
    return name.encode("latin-1")


def _make_scope(
    path: str = "/test",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    scope_type: str = "http",
) -> dict:
    return {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers or [],
    }


async def _simple_asgi_app(scope, receive, send):
    """A trivial ASGI app that returns 200 OK."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [[b"content-type", b"text/plain"]],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


async def _error_asgi_app(scope, receive, send):
    """ASGI app that returns 500."""
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [],
        }
    )
    await send({"type": "http.response.body", "body": b"error"})


class _ContextCapture:
    """ASGI app that captures trace context during the request."""

    def __init__(self):
        self.captured_context = None

    async def __call__(self, scope, receive, send):
        self.captured_context = get_trace_context()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})


async def _noop_receive():
    return {"type": "http.request", "body": b""}


class _SendCollector:
    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message):
        self.messages.append(message)


def test_asgi_middleware_sets_context_during_request():
    """Context should be available inside the ASGI app handler."""
    clear_trace_context()
    client = _StubClient()
    capture_app = _ContextCapture()
    middleware = IncidentaryASGIMiddleware(capture_app, client)

    trace_id = "trace-asgi-1"
    ce_header = "ce-parent-1"
    scope = _make_scope(
        headers=[
            (_encode_header(TRACE_ID_HEADER), trace_id.encode()),
            (_encode_header(PARENT_CE_HEADER), ce_header.encode()),
        ]
    )
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert capture_app.captured_context is not None
    assert capture_app.captured_context.trace_id == trace_id
    clear_trace_context()


def test_asgi_middleware_clears_context_after_request():
    """Context should be cleared after the request completes."""
    clear_trace_context()
    client = _StubClient()
    middleware = IncidentaryASGIMiddleware(_simple_asgi_app, client)
    scope = _make_scope()
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert get_trace_context() is None


def test_asgi_middleware_passes_through_non_http_scopes():
    """Non-http scopes (lifespan, websocket) should pass through unmodified."""
    clear_trace_context()
    client = _StubClient()
    call_count = 0

    async def passthrough_app(scope, receive, send):
        nonlocal call_count
        call_count += 1

    middleware = IncidentaryASGIMiddleware(passthrough_app, client)
    scope = _make_scope(scope_type="lifespan")
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert call_count == 1
    # No events should be recorded for non-http scopes
    assert len(client.events) == 0
    assert len(client.start_kinds) == 0


def test_asgi_middleware_records_request():
    """Middleware should call record_request_start and record_request."""
    clear_trace_context()
    client = _StubClient()
    middleware = IncidentaryASGIMiddleware(_simple_asgi_app, client)
    scope = _make_scope()
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert client.start_kinds == ["HTTP_IN"]
    assert len(client.request_calls) == 1
    status_code, _ = client.request_calls[0]
    assert status_code == 200


def test_asgi_middleware_records_error_status():
    """Middleware should capture 500 status from the response."""
    clear_trace_context()
    client = _StubClient()
    middleware = IncidentaryASGIMiddleware(_error_asgi_app, client)
    scope = _make_scope()
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert len(client.request_calls) == 1
    status_code, _ = client.request_calls[0]
    assert status_code == 500


def test_asgi_middleware_writes_event():
    """Middleware should emit a causal event."""
    clear_trace_context()
    client = _StubClient()
    middleware = IncidentaryASGIMiddleware(_simple_asgi_app, client)
    scope = _make_scope(path="/api/health", method="GET")
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert len(client.events) == 1
    ce = client.events[0]
    assert ce.kind == "HTTP_SERVER"
    assert ce.status_code == 200


def test_asgi_middleware_generates_trace_id_when_missing():
    """If no trace header is present, a new trace_id should be generated."""
    clear_trace_context()
    client = _StubClient()
    middleware = IncidentaryASGIMiddleware(_simple_asgi_app, client)
    scope = _make_scope(headers=[])
    send = _SendCollector()

    asyncio.run(middleware(scope, _noop_receive, send))

    assert len(client.events) == 1
    ce = client.events[0]
    assert ce.trace_id  # non-empty
    assert ce.parent_id is None
