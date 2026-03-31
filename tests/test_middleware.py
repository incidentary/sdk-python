import uuid

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
    trace_id, parent_id = extract_trace_context(
        {TRACE_ID_HEADER: trace, PARENT_CE_HEADER: parent}
    )
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
