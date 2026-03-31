"""Tests for Flask integration (TDD — written before implementation).

Flask is mocked throughout; it does not need to be installed.
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


def _build_fake_flask():
    """Return a minimal fake flask module."""
    flask_mod = MagicMock()

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.wsgi_app = _make_simple_wsgi_app()

    flask_mod.Flask = FakeFlask
    return flask_mod


def _make_simple_wsgi_app():
    """Return a simple callable wsgi app."""
    def app(environ, start_response):
        start_response("200 OK", [])
        return [b"OK"]
    return app


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestFlaskDetect:
    def test_detect_returns_false_when_flask_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_flask_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("flask")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestFlaskName:
    def test_name_is_flask(self):
        from incidentary.integrations.flask import FlaskIntegration

        assert FlaskIntegration().name == "flask"


# ---------------------------------------------------------------------------
# is_patched()
# ---------------------------------------------------------------------------


class TestFlaskIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.flask import FlaskIntegration

        assert FlaskIntegration().is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_flask = _build_fake_flask()
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_flask = _build_fake_flask()
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — Flask.__init__ wrapping
# ---------------------------------------------------------------------------


class TestFlaskPatch:
    def test_patch_replaces_flask_init(self):
        fake_flask = _build_fake_flask()
        original_init = fake_flask.Flask.__init__
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_flask.Flask.__init__ is not original_init

    def test_patch_is_idempotent(self):
        fake_flask = _build_fake_flask()
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)
            patched_init = fake_flask.Flask.__init__
            integration.patch(client)
            # Second patch must not double-wrap
            assert fake_flask.Flask.__init__ is patched_init

    def test_patch_does_not_raise_when_flask_missing(self):
        from incidentary.integrations.flask import FlaskIntegration

        integration = FlaskIntegration()
        client = _make_stub_client()
        with patch.dict(sys.modules, {"flask": None}):
            integration.patch(client)  # must not raise

    def test_new_flask_app_has_wsgi_app_wrapped(self):
        """After patching, new Flask() instances should have their wsgi_app wrapped."""
        fake_flask = _build_fake_flask()
        original_wsgi_app = None

        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration
            from incidentary.middleware import IncidentaryWSGIMiddleware

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)

            # Instantiate a new Flask app after patching
            app = fake_flask.Flask("myapp")

        # wsgi_app should now be an IncidentaryWSGIMiddleware
        assert isinstance(app.wsgi_app, IncidentaryWSGIMiddleware)

    def test_new_flask_app_wsgi_app_wraps_original(self):
        """The wrapped wsgi_app should delegate to the original app."""
        fake_flask = _build_fake_flask()

        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration
            from incidentary.middleware import IncidentaryWSGIMiddleware

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)

            app = fake_flask.Flask("myapp")

        # The middleware should wrap the original wsgi_app
        assert isinstance(app.wsgi_app, IncidentaryWSGIMiddleware)
        assert app.wsgi_app._app is not None


# ---------------------------------------------------------------------------
# unpatch() — restore Flask.__init__
# ---------------------------------------------------------------------------


class TestFlaskUnpatch:
    def test_unpatch_restores_original_flask_init(self):
        fake_flask = _build_fake_flask()
        original_init = fake_flask.Flask.__init__
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

        assert fake_flask.Flask.__init__ is original_init

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.flask import FlaskIntegration

        integration = FlaskIntegration()
        integration.unpatch()  # must not raise

    def test_unpatch_is_idempotent(self):
        fake_flask = _build_fake_flask()
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()  # must not raise

    def test_after_unpatch_new_flask_apps_not_wrapped(self):
        """After unpatch, new Flask() instances should NOT have wrapped wsgi_app."""
        fake_flask = _build_fake_flask()
        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration
            from incidentary.middleware import IncidentaryWSGIMiddleware

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

            app = fake_flask.Flask("myapp")

        assert not isinstance(app.wsgi_app, IncidentaryWSGIMiddleware)


# ---------------------------------------------------------------------------
# OTel conflict detection
# ---------------------------------------------------------------------------


class TestFlaskOtelConflict:
    def test_skips_patching_when_otel_marker_present(self):
        """If Flask.__init__ already has __otel_original, skip patching."""
        fake_flask = _build_fake_flask()
        original_init = fake_flask.Flask.__init__
        setattr(original_init, "__otel_original", True)

        with patch.dict(sys.modules, {"flask": fake_flask}):
            from incidentary.integrations.flask import FlaskIntegration

            integration = FlaskIntegration()
            client = _make_stub_client()
            integration.patch(client)

        # Should not have replaced __init__
        assert fake_flask.Flask.__init__ is original_init

        delattr(original_init, "__otel_original")


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestFlaskABCConformance:
    def test_flask_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.flask import FlaskIntegration

        assert isinstance(FlaskIntegration(), Integration)

    def test_flask_integration_importable_from_integrations_package(self):
        from incidentary.integrations import FlaskIntegration

        assert FlaskIntegration is not None

    def test_default_integrations_includes_flask(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.flask import FlaskIntegration

        result = default_integrations()
        assert any(isinstance(i, FlaskIntegration) for i in result)
