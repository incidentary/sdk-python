"""Tests for HTTP auto-instrumentation (urllib + requests patching)."""

from __future__ import annotations

import http.server
import threading
import urllib.request
from unittest.mock import MagicMock, patch

from incidentary.auto_instrument import auto_instrument, is_patched, undo_patches
from incidentary.context import clear_trace_context, get_trace_context, set_trace_context
from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


class _EchoHeadersHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that echoes back the Incidentary headers it received."""

    received_headers: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        _EchoHeadersHandler.received_headers = {
            k.lower(): v for k, v in self.headers.items()
        }
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress server logs during tests


def _start_test_server() -> tuple[http.server.HTTPServer, str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHeadersHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


class TestUrllibPatching:
    """Tests for urllib.request.urlopen patching."""

    def setup_method(self):
        clear_trace_context()
        undo_patches()
        _EchoHeadersHandler.received_headers = {}

    def teardown_method(self):
        clear_trace_context()
        undo_patches()

    def test_urlopen_with_active_context_injects_headers(self):
        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            set_trace_context("trace-abc", "ce-123")
            urllib.request.urlopen(f"{base_url}/test")  # noqa: S310

            headers = _EchoHeadersHandler.received_headers
            assert headers.get(TRACE_ID_HEADER) == "trace-abc"
            assert headers.get(PARENT_CE_HEADER) == "ce-123"
        finally:
            server.shutdown()

    def test_urlopen_without_context_does_not_inject_headers(self):
        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            clear_trace_context()
            urllib.request.urlopen(f"{base_url}/test")  # noqa: S310

            headers = _EchoHeadersHandler.received_headers
            assert TRACE_ID_HEADER not in headers
            assert PARENT_CE_HEADER not in headers
        finally:
            server.shutdown()

    def test_urlopen_string_url_converted_to_request(self):
        """String URLs should be converted to Request objects for header injection."""
        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            set_trace_context("trace-str", "ce-str")
            urllib.request.urlopen(f"{base_url}/string-url")  # noqa: S310

            headers = _EchoHeadersHandler.received_headers
            assert headers.get(TRACE_ID_HEADER) == "trace-str"
        finally:
            server.shutdown()

    def test_urlopen_request_object_gets_headers_injected(self):
        """Request objects passed directly should also get headers."""
        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            set_trace_context("trace-req", "ce-req")
            req = urllib.request.Request(f"{base_url}/request-obj")
            urllib.request.urlopen(req)  # noqa: S310

            headers = _EchoHeadersHandler.received_headers
            assert headers.get(TRACE_ID_HEADER) == "trace-req"
            assert headers.get(PARENT_CE_HEADER) == "ce-req"
        finally:
            server.shutdown()

    def test_urlopen_records_http_out_event(self):
        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            set_trace_context("trace-event", "ce-event")
            urllib.request.urlopen(f"{base_url}/test")  # noqa: S310

            client.record_event.assert_called_once()
            call_args = client.record_event.call_args
            assert call_args[0][0] == "http_out"
        finally:
            server.shutdown()

    def test_undo_patches_restores_original_urlopen(self):
        server, base_url = _start_test_server()
        try:
            original_urlopen = urllib.request.urlopen

            client = _make_stub_client()
            auto_instrument(client)
            assert urllib.request.urlopen is not original_urlopen

            undo_patches()
            assert urllib.request.urlopen is original_urlopen

            # After undo, no headers should be injected even with context
            set_trace_context("trace-undo", "ce-undo")
            urllib.request.urlopen(f"{base_url}/test")  # noqa: S310

            headers = _EchoHeadersHandler.received_headers
            assert TRACE_ID_HEADER not in headers
        finally:
            server.shutdown()


class TestIdempotency:
    """Test that auto_instrument is idempotent."""

    def setup_method(self):
        clear_trace_context()
        undo_patches()

    def teardown_method(self):
        clear_trace_context()
        undo_patches()

    def test_calling_auto_instrument_twice_does_not_double_patch(self):
        original_urlopen = urllib.request.urlopen

        client = _make_stub_client()
        auto_instrument(client)
        patched_once = urllib.request.urlopen

        auto_instrument(client)
        patched_twice = urllib.request.urlopen

        # Should be the same wrapper, not a double-wrapped function
        assert patched_once is patched_twice
        assert patched_once is not original_urlopen

    def test_is_patched_reflects_state(self):
        assert is_patched() is False
        client = _make_stub_client()
        auto_instrument(client)
        assert is_patched() is True
        undo_patches()
        assert is_patched() is False


def test_skips_urllib_patching_when_otel_present():
    """Test that OTel conflict detection works.

    NOTE: This test is a standalone function (not inside a class) because
    Python's name-mangling rewrites ``__otel_original`` to
    ``_ClassName__otel_original`` inside class bodies, which defeats the
    attribute check we need to exercise.
    """
    clear_trace_context()
    undo_patches()

    _otel_attr = "__otel_original"
    original = urllib.request.urlopen
    # Simulate OTel having patched urlopen
    setattr(urllib.request.urlopen, _otel_attr, True)
    try:
        client = _make_stub_client()
        auto_instrument(client)

        # urllib should NOT have been patched (OTel conflict)
        assert urllib.request.urlopen is original
    finally:
        # Clean up the OTel marker
        if hasattr(urllib.request.urlopen, _otel_attr):
            delattr(urllib.request.urlopen, _otel_attr)
        clear_trace_context()
        undo_patches()


class TestRequestsPatching:
    """Tests for requests library patching (conditional)."""

    def setup_method(self):
        clear_trace_context()
        undo_patches()

    def teardown_method(self):
        clear_trace_context()
        undo_patches()

    def test_requests_with_active_context_injects_headers(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            import pytest
            pytest.skip("requests not installed")

        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            set_trace_context("trace-req-lib", "ce-req-lib")
            resp = requests.get(f"{base_url}/test")

            headers = _EchoHeadersHandler.received_headers
            assert headers.get(TRACE_ID_HEADER) == "trace-req-lib"
            assert headers.get(PARENT_CE_HEADER) == "ce-req-lib"
            assert resp.status_code == 200
        finally:
            server.shutdown()

    def test_requests_without_context_does_not_inject_headers(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            import pytest
            pytest.skip("requests not installed")

        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            clear_trace_context()
            resp = requests.get(f"{base_url}/test")

            headers = _EchoHeadersHandler.received_headers
            assert TRACE_ID_HEADER not in headers
            assert PARENT_CE_HEADER not in headers
            assert resp.status_code == 200
        finally:
            server.shutdown()

    def test_requests_records_http_out_event(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            import pytest
            pytest.skip("requests not installed")

        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            auto_instrument(client)

            set_trace_context("trace-req-ev", "ce-req-ev")
            requests.get(f"{base_url}/test")

            client.record_event.assert_called_once()
            call_args = client.record_event.call_args
            assert call_args[0][0] == "http_out"
        finally:
            server.shutdown()


class TestNoThrowGuarantee:
    """Auto-instrumentation must never throw into user code."""

    def setup_method(self):
        clear_trace_context()
        undo_patches()

    def teardown_method(self):
        clear_trace_context()
        undo_patches()

    def test_patching_does_not_throw_when_requests_not_installed(self):
        """If requests is not importable, auto_instrument should not fail."""
        client = _make_stub_client()
        with patch.dict("sys.modules", {"requests": None}):
            # Should not raise even if requests import fails
            auto_instrument(client)
            # urllib should still be patched
            assert is_patched() is True

    def test_record_event_failure_does_not_break_urlopen(self):
        """If client.record_event raises, urlopen should still work."""
        server, base_url = _start_test_server()
        try:
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            auto_instrument(client)

            set_trace_context("trace-err", "ce-err")
            # Should not raise despite record_event failure
            response = urllib.request.urlopen(f"{base_url}/test")  # noqa: S310
            assert response.read() == b"ok"
        finally:
            server.shutdown()
