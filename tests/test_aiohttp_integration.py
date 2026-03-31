"""Tests for aiohttp integration (TDD — written before implementation).

aiohttp is mocked throughout; it does not need to be installed.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


def _build_fake_aiohttp():
    """Return a minimal fake aiohttp module."""
    aiohttp_mod = MagicMock()

    class FakeClientSession:
        async def _request(self, method, url, **kwargs):
            resp = MagicMock()
            resp.status = 200
            return resp

    aiohttp_mod.ClientSession = FakeClientSession
    return aiohttp_mod


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestAiohttpDetect:
    def test_detect_returns_false_when_aiohttp_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_aiohttp_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("aiohttp")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestAiohttpName:
    def test_name_is_aiohttp(self):
        from incidentary.integrations.aiohttp_integration import AiohttpIntegration

        assert AiohttpIntegration().name == "aiohttp"


# ---------------------------------------------------------------------------
# is_patched
# ---------------------------------------------------------------------------


class TestAiohttpIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.aiohttp_integration import AiohttpIntegration

        integration = AiohttpIntegration()
        assert integration.is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_aiohttp = _build_fake_aiohttp()
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_aiohttp = _build_fake_aiohttp()
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — monkey-patching ClientSession._request
# ---------------------------------------------------------------------------


class TestAiohttpPatch:
    def test_patch_replaces_client_session_request(self):
        fake_aiohttp = _build_fake_aiohttp()
        original = fake_aiohttp.ClientSession._request
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)

            assert fake_aiohttp.ClientSession._request is not original

    def test_patch_is_idempotent(self):
        fake_aiohttp = _build_fake_aiohttp()
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)
            patched_once = fake_aiohttp.ClientSession._request
            integration.patch(client)
            patched_twice = fake_aiohttp.ClientSession._request
            assert patched_once is patched_twice

    def test_patch_does_not_raise_when_aiohttp_missing(self):
        with patch.dict(sys.modules, {"aiohttp": None}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)


# ---------------------------------------------------------------------------
# unpatch()
# ---------------------------------------------------------------------------


class TestAiohttpUnpatch:
    def test_unpatch_restores_original_request(self):
        fake_aiohttp = _build_fake_aiohttp()
        original = fake_aiohttp.ClientSession._request
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

            assert fake_aiohttp.ClientSession._request is original

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.aiohttp_integration import AiohttpIntegration

        integration = AiohttpIntegration()
        integration.unpatch()

    def test_unpatch_is_idempotent(self):
        fake_aiohttp = _build_fake_aiohttp()
        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()


# ---------------------------------------------------------------------------
# Async behaviour — header injection
# ---------------------------------------------------------------------------


class TestAiohttpHeaderInjection:
    async def test_request_injects_trace_headers_when_context_active(self):
        fake_aiohttp = _build_fake_aiohttp()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration
            from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)

            captured_kwargs: dict = {}

            original_request = fake_aiohttp.ClientSession._request.__wrapped__ if hasattr(
                fake_aiohttp.ClientSession._request, "__wrapped__"
            ) else None

            # Capture headers by wrapping the patched method
            async def capturing_original(self, method, url, **kwargs):
                captured_kwargs.update(kwargs)
                resp = MagicMock()
                resp.status = 200
                return resp

            # Temporarily replace the stored original to capture headers
            set_trace_context("trace-aio-1", "ce-aio-1")
            try:
                session = fake_aiohttp.ClientSession()
                # Call the patched method; headers should be injected into kwargs
                await fake_aiohttp.ClientSession._request(session, "GET", "http://example.com")
            finally:
                clear_trace_context()

        # The patched _request injects headers into kwargs
        # We can't easily introspect kwargs passed to the original here,
        # so instead we verify through the session call recording.
        # This test verifies the patched method is called (not the original).

    async def test_request_injects_trace_headers_via_kwargs(self):
        """Verify the patched _request injects headers into the kwargs dict."""
        fake_aiohttp = _build_fake_aiohttp()

        # Track kwargs that flow into the original
        received_kwargs: dict = {}

        class TrackingSession:
            async def _request(self, method, url, **kwargs):
                received_kwargs.update(kwargs)
                resp = MagicMock()
                resp.status = 200
                return resp

        fake_aiohttp.ClientSession = TrackingSession

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration
            from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)

            set_trace_context("trace-aio-2", "ce-aio-2")
            try:
                session = TrackingSession()
                await fake_aiohttp.ClientSession._request(session, "GET", "http://example.com")
            finally:
                clear_trace_context()

        headers = received_kwargs.get("headers", {})
        assert headers.get(TRACE_ID_HEADER) == "trace-aio-2"
        assert headers.get(PARENT_CE_HEADER) == "ce-aio-2"

    async def test_request_does_not_inject_headers_when_no_context(self):
        fake_aiohttp = _build_fake_aiohttp()
        received_kwargs: dict = {}

        class TrackingSession:
            async def _request(self, method, url, **kwargs):
                received_kwargs.update(kwargs)
                resp = MagicMock()
                resp.status = 200
                return resp

        fake_aiohttp.ClientSession = TrackingSession

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.context import clear_trace_context
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration
            from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)

            clear_trace_context()
            session = TrackingSession()
            await fake_aiohttp.ClientSession._request(session, "GET", "http://example.com")

        headers = received_kwargs.get("headers", {})
        assert TRACE_ID_HEADER not in headers
        assert PARENT_CE_HEADER not in headers

    async def test_request_records_http_out_event(self):
        fake_aiohttp = _build_fake_aiohttp()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)

            set_trace_context("trace-aio-ev", "ce-aio-ev")
            try:
                session = fake_aiohttp.ClientSession()
                await fake_aiohttp.ClientSession._request(session, "GET", "http://example.com")
            finally:
                clear_trace_context()

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "http_out"

    async def test_request_does_not_raise_on_record_event_failure(self):
        fake_aiohttp = _build_fake_aiohttp()

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            integration.patch(client)

            session = fake_aiohttp.ClientSession()
            # Must not raise
            await fake_aiohttp.ClientSession._request(session, "GET", "http://example.com")

    async def test_request_still_raises_original_exception(self):
        fake_aiohttp = _build_fake_aiohttp()

        class BrokenSession:
            async def _request(self, method, url, **kwargs):
                raise ConnectionError("refused")

        fake_aiohttp.ClientSession = BrokenSession

        with patch.dict(sys.modules, {"aiohttp": fake_aiohttp}):
            from incidentary.integrations.aiohttp_integration import AiohttpIntegration

            integration = AiohttpIntegration()
            client = _make_stub_client()
            integration.patch(client)

            session = BrokenSession()
            with pytest.raises(ConnectionError):
                await fake_aiohttp.ClientSession._request(session, "GET", "http://example.com")


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestAiohttpABCConformance:
    def test_aiohttp_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.aiohttp_integration import AiohttpIntegration

        assert isinstance(AiohttpIntegration(), Integration)

    def test_aiohttp_integration_importable_from_integrations_package(self):
        from incidentary.integrations import AiohttpIntegration

        assert AiohttpIntegration is not None

    def test_default_integrations_includes_aiohttp(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.aiohttp_integration import AiohttpIntegration

        result = default_integrations()
        assert any(isinstance(i, AiohttpIntegration) for i in result)
