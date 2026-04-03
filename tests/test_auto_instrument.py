"""Tests for HTTP auto-instrumentation (urllib + requests patching)."""

from __future__ import annotations

import http.server
import threading
import urllib.request
from unittest.mock import MagicMock, patch

from incidentary.auto_instrument import auto_instrument, is_patched, undo_patches
from incidentary.context import clear_trace_context, set_trace_context
from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


class _EchoHeadersHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that echoes back the Incidentary headers it received."""

    received_headers: dict[str, str] = {}

    def do_GET(self):
        _EchoHeadersHandler.received_headers = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
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
            urllib.request.urlopen(f"{base_url}/test")

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
            urllib.request.urlopen(f"{base_url}/test")

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
            urllib.request.urlopen(f"{base_url}/string-url")

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
            urllib.request.urlopen(req)

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
            urllib.request.urlopen(f"{base_url}/test")

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
            urllib.request.urlopen(f"{base_url}/test")

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
            import requests
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
            import requests
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
            import requests
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
            response = urllib.request.urlopen(f"{base_url}/test")
            assert response.read() == b"ok"
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# _record_http_out edge cases
# ---------------------------------------------------------------------------


def _get_ai_module():
    """Get the auto_instrument module (not the function) via sys.modules."""
    import sys

    return sys.modules["incidentary.auto_instrument"]


class TestRecordHttpOut:
    """Tests for the _record_http_out helper function."""

    def setup_method(self):
        clear_trace_context()
        undo_patches()

    def teardown_method(self):
        clear_trace_context()
        undo_patches()

    def test_record_http_out_no_client(self):
        """When _client_ref is None, _record_http_out should not raise."""
        from incidentary.auto_instrument import _record_http_out

        # Should not raise
        _record_http_out(start_ns=0, status_code=200)

    def test_record_http_out_with_client_and_context(self):
        """When client and context are available, record_event should be called."""
        ai_mod = _get_ai_module()
        client = _make_stub_client()
        ai_mod._client_ref = client

        set_trace_context("trace-rec", "ce-rec")
        ai_mod._record_http_out(start_ns=0, status_code=200)

        client.record_event.assert_called_once()
        call_args = client.record_event.call_args
        assert call_args[0][0] == "http_out"

        ai_mod._client_ref = None

    def test_record_http_out_without_context(self):
        """When no trace context, record_event should still be called."""
        ai_mod = _get_ai_module()
        client = _make_stub_client()
        ai_mod._client_ref = client

        clear_trace_context()
        ai_mod._record_http_out(start_ns=0, status_code=500)

        client.record_event.assert_called_once()
        opts = client.record_event.call_args[0][1]
        assert opts.trace_id is None
        assert opts.parent_ce_id is None

        ai_mod._client_ref = None

    def test_record_http_out_exception_in_record_event(self):
        """If record_event raises, _record_http_out must not propagate."""
        ai_mod = _get_ai_module()
        client = _make_stub_client()
        client.record_event.side_effect = RuntimeError("boom")
        ai_mod._client_ref = client

        # Should not raise
        ai_mod._record_http_out(start_ns=0, status_code=200)
        ai_mod._client_ref = None


# ---------------------------------------------------------------------------
# urllib error status propagation
# ---------------------------------------------------------------------------


class TestUrllibErrorPropagation:
    """Test that HTTP errors during urlopen propagate correctly."""

    def setup_method(self):
        clear_trace_context()
        undo_patches()

    def teardown_method(self):
        clear_trace_context()
        undo_patches()

    def test_urlopen_http_error_reraises_and_records(self):
        """urllib HTTPErrors should re-raise but still record the event."""
        import io
        import urllib.error

        import pytest

        def failing_urlopen(url, data=None, *args, **kwargs):
            raise urllib.error.HTTPError(
                url="http://test",
                code=502,
                msg="Bad Gateway",
                hdrs=None,
                fp=io.BytesIO(b"error"),
            )

        # Replace urlopen *before* auto_instrument so the closure captures it
        original = urllib.request.urlopen
        urllib.request.urlopen = failing_urlopen
        try:
            client = _make_stub_client()
            auto_instrument(client)
            set_trace_context("trace-err", "ce-err")

            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen("http://test/fail")

            client.record_event.assert_called_once()
            opts = client.record_event.call_args[0][1]
            assert opts.status == 502
        finally:
            undo_patches()
            urllib.request.urlopen = original

    def test_urlopen_generic_error_reraises_and_records(self):
        """Non-HTTP errors should re-raise and record status 0."""
        import pytest

        def failing_urlopen(url, data=None, *args, **kwargs):
            raise ConnectionError("connection refused")

        original = urllib.request.urlopen
        urllib.request.urlopen = failing_urlopen
        try:
            client = _make_stub_client()
            auto_instrument(client)
            set_trace_context("trace-conn", "ce-conn")

            with pytest.raises(ConnectionError):
                urllib.request.urlopen("http://test/fail")

            client.record_event.assert_called_once()
            opts = client.record_event.call_args[0][1]
            # status_code=0 maps to status=None (only set when > 0)
            assert opts.status is None
        finally:
            undo_patches()
            urllib.request.urlopen = original
