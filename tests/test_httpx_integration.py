"""Tests for httpx integration (TDD — written before implementation).

httpx is mocked throughout; it does not need to be installed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


def _build_fake_httpx():
    """Return a minimal fake httpx module."""
    httpx_mod = MagicMock()

    class FakeHeaders(dict):
        pass

    class FakeRequest:
        def __init__(self):
            self.headers = FakeHeaders()

    class FakeResponse:
        def __init__(self, status_code=200):
            self.status_code = status_code

    class FakeHTTPTransport:
        def handle_request(self, request):
            return FakeResponse(200)

    class FakeAsyncHTTPTransport:
        async def handle_async_request(self, request):
            return FakeResponse(200)

    httpx_mod.HTTPTransport = FakeHTTPTransport
    httpx_mod.AsyncHTTPTransport = FakeAsyncHTTPTransport
    httpx_mod._models = MagicMock()
    httpx_mod._models.Request = FakeRequest
    httpx_mod._models.Response = FakeResponse
    return httpx_mod


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestHttpxDetect:
    def test_detect_returns_false_when_httpx_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_httpx_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("httpx")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestHttpxName:
    def test_name_is_httpx(self):
        from incidentary.integrations.httpx_integration import HttpxIntegration

        assert HttpxIntegration().name == "httpx"


# ---------------------------------------------------------------------------
# is_patched
# ---------------------------------------------------------------------------


class TestHttpxIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.httpx_integration import HttpxIntegration

        integration = HttpxIntegration()
        assert integration.is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_httpx = _build_fake_httpx()
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_httpx = _build_fake_httpx()
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — sync transport
# ---------------------------------------------------------------------------


class TestHttpxSyncPatch:
    def test_patch_replaces_handle_request(self):
        fake_httpx = _build_fake_httpx()
        original = fake_httpx.HTTPTransport.handle_request
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            assert fake_httpx.HTTPTransport.handle_request is not original

    def test_patch_is_idempotent(self):
        fake_httpx = _build_fake_httpx()
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)
            patched_once = fake_httpx.HTTPTransport.handle_request
            integration.patch(client)
            patched_twice = fake_httpx.HTTPTransport.handle_request
            assert patched_once is patched_twice

    def test_patch_does_not_raise_when_httpx_missing(self):
        with patch.dict(sys.modules, {"httpx": None}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)


# ---------------------------------------------------------------------------
# unpatch() — sync transport
# ---------------------------------------------------------------------------


class TestHttpxUnpatch:
    def test_unpatch_restores_original_handle_request(self):
        fake_httpx = _build_fake_httpx()
        original = fake_httpx.HTTPTransport.handle_request
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

            assert fake_httpx.HTTPTransport.handle_request is original

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.httpx_integration import HttpxIntegration

        integration = HttpxIntegration()
        integration.unpatch()

    def test_unpatch_is_idempotent(self):
        fake_httpx = _build_fake_httpx()
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()


# ---------------------------------------------------------------------------
# Sync header injection behaviour
# ---------------------------------------------------------------------------


class TestHttpxSyncHeaderInjection:
    def test_handle_request_injects_trace_headers_when_context_active(self):
        fake_httpx = _build_fake_httpx()
        injected_headers = {}

        def recording_handle_request(self, request):
            injected_headers.update(dict(request.headers))
            return fake_httpx.HTTPTransport.handle_request.__wrapped__(self, request)

        # We'll capture by inspecting the request passed to the original

        class CapturingTransport(fake_httpx.HTTPTransport):
            def handle_request(self, request):
                injected_headers.update(dict(request.headers))
                return type("Resp", (), {"status_code": 200})()

        fake_httpx.HTTPTransport = CapturingTransport

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.httpx_integration import HttpxIntegration
            from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            set_trace_context("trace-httpx-1", "ce-httpx-1")
            try:
                transport = CapturingTransport()
                fake_httpx.HTTPTransport.handle_request(transport, request)
            finally:
                clear_trace_context()

        assert request.headers.get(TRACE_ID_HEADER) == "trace-httpx-1"
        assert request.headers.get(PARENT_CE_HEADER) == "ce-httpx-1"

    def test_handle_request_does_not_inject_headers_when_no_context(self):
        fake_httpx = _build_fake_httpx()

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.context import clear_trace_context
            from incidentary.integrations.httpx_integration import HttpxIntegration
            from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            clear_trace_context()
            transport = fake_httpx.HTTPTransport()
            fake_httpx.HTTPTransport.handle_request(transport, request)

        assert TRACE_ID_HEADER not in request.headers
        assert PARENT_CE_HEADER not in request.headers

    def test_handle_request_records_http_out_event(self):
        fake_httpx = _build_fake_httpx()

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            set_trace_context("trace-httpx-ev", "ce-httpx-ev")
            try:
                transport = fake_httpx.HTTPTransport()
                fake_httpx.HTTPTransport.handle_request(transport, request)
            finally:
                clear_trace_context()

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "http_out"

    def test_handle_request_does_not_raise_on_record_event_failure(self):
        fake_httpx = _build_fake_httpx()

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            transport = fake_httpx.HTTPTransport()
            # Must not raise
            fake_httpx.HTTPTransport.handle_request(transport, request)

    def test_handle_request_still_raises_original_exception(self):
        """Exceptions from the underlying transport must propagate."""
        fake_httpx = _build_fake_httpx()

        class BrokenTransport(fake_httpx.HTTPTransport):
            def handle_request(self, request):
                raise ConnectionError("refused")

        fake_httpx.HTTPTransport = BrokenTransport

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            transport = BrokenTransport()
            with pytest.raises(ConnectionError):
                fake_httpx.HTTPTransport.handle_request(transport, request)


# ---------------------------------------------------------------------------
# Async transport patching behaviour
# ---------------------------------------------------------------------------


class TestHttpxAsyncPatch:
    def test_patch_replaces_handle_async_request(self):
        fake_httpx = _build_fake_httpx()
        original = fake_httpx.AsyncHTTPTransport.handle_async_request
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            assert fake_httpx.AsyncHTTPTransport.handle_async_request is not original

    def test_unpatch_restores_handle_async_request(self):
        fake_httpx = _build_fake_httpx()
        original = fake_httpx.AsyncHTTPTransport.handle_async_request
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

            assert fake_httpx.AsyncHTTPTransport.handle_async_request is original

    async def test_handle_async_request_injects_trace_headers(self):
        fake_httpx = _build_fake_httpx()

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.httpx_integration import HttpxIntegration
            from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            set_trace_context("trace-async-httpx", "ce-async-httpx")
            try:
                transport = fake_httpx.AsyncHTTPTransport()
                await fake_httpx.AsyncHTTPTransport.handle_async_request(transport, request)
            finally:
                clear_trace_context()

        assert request.headers.get(TRACE_ID_HEADER) == "trace-async-httpx"
        assert request.headers.get(PARENT_CE_HEADER) == "ce-async-httpx"

    async def test_handle_async_request_records_http_out_event(self):
        fake_httpx = _build_fake_httpx()

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            request = MagicMock()
            request.headers = {}

            set_trace_context("trace-async-ev", "ce-async-ev")
            try:
                transport = fake_httpx.AsyncHTTPTransport()
                await fake_httpx.AsyncHTTPTransport.handle_async_request(transport, request)
            finally:
                clear_trace_context()

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "http_out"


# ---------------------------------------------------------------------------
# OTel conflict detection
# ---------------------------------------------------------------------------


class TestHttpxOtelConflict:
    def test_skips_sync_patching_when_otel_marker_present(self):
        fake_httpx = _build_fake_httpx()
        original = fake_httpx.HTTPTransport.handle_request
        # Simulate OTel already patched
        setattr(original, "__otel_original", True)

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from incidentary.integrations.httpx_integration import HttpxIntegration

            integration = HttpxIntegration()
            client = _make_stub_client()
            integration.patch(client)

            # Should not have replaced the method
            assert fake_httpx.HTTPTransport.handle_request is original

        delattr(original, "__otel_original")


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestHttpxABCConformance:
    def test_httpx_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.httpx_integration import HttpxIntegration

        assert isinstance(HttpxIntegration(), Integration)

    def test_httpx_integration_importable_from_integrations_package(self):
        from incidentary.integrations import HttpxIntegration

        assert HttpxIntegration is not None

    def test_default_integrations_includes_httpx(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.httpx_integration import HttpxIntegration

        result = default_integrations()
        assert any(isinstance(i, HttpxIntegration) for i in result)
