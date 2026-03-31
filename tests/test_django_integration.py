"""Tests for Django integration (TDD — written before implementation).

Django is mocked throughout; it does not need to be installed.
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


def _build_fake_django(middleware_list=None):
    """Return a minimal fake django module with configurable MIDDLEWARE."""
    django_mod = MagicMock()
    django_conf = MagicMock()

    settings = MagicMock()
    settings.configured = True
    settings.MIDDLEWARE = list(middleware_list or [])

    django_conf.settings = settings
    django_mod.conf = django_conf
    return django_mod, settings


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestDjangoDetect:
    def test_detect_returns_false_when_django_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_django_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("django")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestDjangoName:
    def test_name_is_django(self):
        from incidentary.integrations.django import DjangoIntegration

        assert DjangoIntegration().name == "django"


# ---------------------------------------------------------------------------
# is_patched()
# ---------------------------------------------------------------------------


class TestDjangoIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.django import DjangoIntegration

        assert DjangoIntegration().is_patched() is False

    def test_is_patched_true_after_patch(self):
        django_mod, settings = _build_fake_django()
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        django_mod, settings = _build_fake_django()
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — middleware injection
# ---------------------------------------------------------------------------


class TestDjangoPatch:
    def test_patch_inserts_middleware_at_position_0(self):
        django_mod, settings = _build_fake_django(["existing.Middleware"])
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert settings.MIDDLEWARE[0] == "incidentary.integrations.django.IncidentaryDjangoMiddleware"

    def test_patch_preserves_existing_middleware(self):
        existing = ["existing.Middleware", "other.Middleware"]
        django_mod, settings = _build_fake_django(existing)
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert "existing.Middleware" in settings.MIDDLEWARE
        assert "other.Middleware" in settings.MIDDLEWARE

    def test_patch_is_idempotent(self):
        django_mod, settings = _build_fake_django()
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.patch(client)  # second call should be a no-op

        count = settings.MIDDLEWARE.count("incidentary.integrations.django.IncidentaryDjangoMiddleware")
        assert count == 1

    def test_patch_skips_when_settings_not_configured(self):
        django_mod, settings = _build_fake_django()
        settings.configured = False
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert "incidentary.integrations.django.IncidentaryDjangoMiddleware" not in settings.MIDDLEWARE

    def test_patch_does_not_raise_when_django_missing(self):
        from incidentary.integrations.django import DjangoIntegration

        integration = DjangoIntegration()
        client = _make_stub_client()
        # patch() must not raise even if django.conf raises
        with patch.dict(sys.modules, {"django": None, "django.conf": None}):
            integration.patch(client)  # no exception

    def test_patch_does_not_raise_when_settings_import_fails(self):
        from incidentary.integrations.django import DjangoIntegration

        integration = DjangoIntegration()
        client = _make_stub_client()
        integration.patch(client)  # django not actually installed, must not raise


# ---------------------------------------------------------------------------
# unpatch() — middleware removal
# ---------------------------------------------------------------------------


class TestDjangoUnpatch:
    def test_unpatch_removes_middleware_from_list(self):
        django_mod, settings = _build_fake_django()
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

        assert "incidentary.integrations.django.IncidentaryDjangoMiddleware" not in settings.MIDDLEWARE

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.django import DjangoIntegration

        integration = DjangoIntegration()
        integration.unpatch()  # must not raise

    def test_unpatch_is_idempotent(self):
        django_mod, settings = _build_fake_django()
        with patch.dict(sys.modules, {"django": django_mod, "django.conf": django_mod.conf}):
            from incidentary.integrations.django import DjangoIntegration

            integration = DjangoIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()  # second call should not raise


# ---------------------------------------------------------------------------
# IncidentaryDjangoMiddleware — request handling
# ---------------------------------------------------------------------------


class TestDjangoMiddlewareRequestHandling:
    def test_middleware_calls_get_response(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware

        response = MagicMock()
        response.status_code = 200
        get_response = MagicMock(return_value=response)

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(get_response)
            request = MagicMock()
            request.META = {}
            mw(request)

        get_response.assert_called_once_with(request)

    def test_middleware_returns_response(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware

        expected_response = MagicMock()
        expected_response.status_code = 200
        get_response = MagicMock(return_value=expected_response)

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(get_response)
            request = MagicMock()
            request.META = {}
            result = mw(request)

        assert result is expected_response

    def test_middleware_extracts_trace_id_from_header(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware
        from incidentary.context import get_trace_context, clear_trace_context

        captured_ctx = []

        def capturing_get_response(req):
            captured_ctx.append(get_trace_context())
            resp = MagicMock()
            resp.status_code = 200
            return resp

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(capturing_get_response)
            request = MagicMock()
            request.META = {"HTTP_X_INCIDENTARY_TRACE_ID": "trace-django-123"}
            mw(request)

        assert len(captured_ctx) == 1
        assert captured_ctx[0] is not None
        assert captured_ctx[0].trace_id == "trace-django-123"

    def test_middleware_generates_trace_id_when_no_header(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware
        from incidentary.context import get_trace_context

        captured_ctx = []

        def capturing_get_response(req):
            captured_ctx.append(get_trace_context())
            resp = MagicMock()
            resp.status_code = 200
            return resp

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(capturing_get_response)
            request = MagicMock()
            request.META = {}
            mw(request)

        assert len(captured_ctx) == 1
        assert captured_ctx[0] is not None
        assert len(captured_ctx[0].trace_id) > 0

    def test_middleware_records_http_in_event(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware

        response = MagicMock()
        response.status_code = 200
        get_response = MagicMock(return_value=response)

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(get_response)
            request = MagicMock()
            request.META = {}
            mw(request)

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "http_in"

    def test_middleware_records_500_on_exception(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware

        def failing_get_response(req):
            raise ValueError("boom")

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(failing_get_response)
            request = MagicMock()
            request.META = {}
            with pytest.raises(ValueError):
                mw(request)

        client.record_event.assert_called()
        opts = client.record_event.call_args[0][1]
        assert opts.status == 500

    def test_middleware_clears_context_after_request(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware
        from incidentary.context import get_trace_context

        response = MagicMock()
        response.status_code = 200
        get_response = MagicMock(return_value=response)

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(get_response)
            request = MagicMock()
            request.META = {"HTTP_X_INCIDENTARY_TRACE_ID": "trace-clear-test"}
            mw(request)

        assert get_trace_context() is None

    def test_middleware_clears_context_even_on_exception(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware
        from incidentary.context import get_trace_context

        def failing_get_response(req):
            raise RuntimeError("error")

        client = _make_stub_client()
        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(failing_get_response)
            request = MagicMock()
            request.META = {"HTTP_X_INCIDENTARY_TRACE_ID": "trace-exc-test"}
            with pytest.raises(RuntimeError):
                mw(request)

        assert get_trace_context() is None

    def test_middleware_does_not_raise_when_client_none(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware

        response = MagicMock()
        response.status_code = 200
        get_response = MagicMock(return_value=response)

        with patch("incidentary.integrations.django._get_client", return_value=None):
            mw = IncidentaryDjangoMiddleware(get_response)
            request = MagicMock()
            request.META = {}
            result = mw(request)  # must not raise

        assert result is response

    def test_middleware_does_not_raise_when_record_event_fails(self):
        from incidentary.integrations.django import IncidentaryDjangoMiddleware

        response = MagicMock()
        response.status_code = 200
        get_response = MagicMock(return_value=response)

        client = _make_stub_client()
        client.record_event.side_effect = RuntimeError("record failed")

        with patch("incidentary.integrations.django._get_client", return_value=client):
            mw = IncidentaryDjangoMiddleware(get_response)
            request = MagicMock()
            request.META = {}
            result = mw(request)  # must not raise

        assert result is response


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestDjangoABCConformance:
    def test_django_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.django import DjangoIntegration

        assert isinstance(DjangoIntegration(), Integration)

    def test_django_integration_importable_from_integrations_package(self):
        from incidentary.integrations import DjangoIntegration

        assert DjangoIntegration is not None

    def test_default_integrations_includes_django(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.django import DjangoIntegration

        result = default_integrations()
        assert any(isinstance(i, DjangoIntegration) for i in result)
