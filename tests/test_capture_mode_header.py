"""Tests for X-Capture-Mode-Requested header propagation.

Transport reads the header from 2xx responses and notifies the client
via callback. Transport does NOT directly mutate capture mode.
"""

from __future__ import annotations

import io
import json
import logging

from incidentary.transport import Transport
from incidentary.types import SkeletonCe


def _make_ce(**overrides) -> SkeletonCe:
    defaults = {
        "id": "ce-1",
        "trace_id": "trace-1",
        "parent_id": None,
        "service_id": "svc",
        "occurred_at": 1_000_000_000,
        "kind": "HTTP_SERVER",
        "status_code": 200,
        "duration_ns": 1_000,
    }
    defaults.update(overrides)
    return SkeletonCe(**defaults)


def _fake_response(status=200, body=b"", headers=None):
    """Build a fake HTTP response with optional headers."""

    class FakeResponse:
        def __init__(self):
            self.status = status
            self._body = body
            self._headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return self._body

        def getheader(self, name, default=None):
            # Case-insensitive header lookup
            for key, value in self._headers.items():
                if key.lower() == name.lower():
                    return value
            return default

    return FakeResponse()


# ---------------------------------------------------------------------------
# Transport: reads X-Capture-Mode-Requested header on 2xx
# ---------------------------------------------------------------------------


class TestTransportCaptureRequestedHeader:
    def test_callback_called_with_header_value_on_2xx(self, monkeypatch):
        """When backend returns X-Capture-Mode-Requested, on_capture_mode_requested fires."""
        received = []
        resp = _fake_response(
            status=200,
            headers={"X-Capture-Mode-Requested": "FULL"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        t = Transport(
            base_url="http://localhost",
            api_key="key",
            on_capture_mode_requested=received.append,
        )
        t._do_upload(b"{}", None)

        assert received == ["FULL"]

    def test_callback_not_called_when_header_absent(self, monkeypatch):
        """When response has no X-Capture-Mode-Requested header, callback is not called."""
        received = []
        resp = _fake_response(status=200, headers={})
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        t = Transport(
            base_url="http://localhost",
            api_key="key",
            on_capture_mode_requested=received.append,
        )
        t._do_upload(b"{}", None)

        assert received == []

    def test_callback_not_called_when_no_callback_registered(self, monkeypatch):
        """When on_capture_mode_requested is None, no error on 2xx with the header."""
        resp = _fake_response(
            status=200,
            headers={"X-Capture-Mode-Requested": "FULL"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        t = Transport(base_url="http://localhost", api_key="key")
        # Should not raise
        t._do_upload(b"{}", None)

    def test_callback_receives_pre_armed_value(self, monkeypatch):
        """Header value PRE_ARMED is forwarded as-is."""
        received = []
        resp = _fake_response(
            status=200,
            headers={"X-Capture-Mode-Requested": "PRE_ARMED"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        t = Transport(
            base_url="http://localhost",
            api_key="key",
            on_capture_mode_requested=received.append,
        )
        t._do_upload(b"{}", None)

        assert received == ["PRE_ARMED"]

    def test_callback_exception_is_swallowed(self, monkeypatch):
        """If the callback raises, _do_upload still completes without error."""
        def bad_callback(mode):
            raise ValueError("boom")

        resp = _fake_response(
            status=200,
            headers={"X-Capture-Mode-Requested": "FULL"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        t = Transport(
            base_url="http://localhost",
            api_key="key",
            on_capture_mode_requested=bad_callback,
        )
        # Should not raise
        t._do_upload(b"{}", None)
        assert t._backend_healthy is True

    def test_header_not_read_on_non_2xx(self, monkeypatch):
        """On retry failure, the header callback is never called."""
        received = []
        monkeypatch.setattr("incidentary.transport.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused")),
        )

        t = Transport(
            base_url="http://localhost",
            api_key="key",
            on_capture_mode_requested=received.append,
        )
        t._do_upload(b"{}", None)

        assert received == []

    def test_header_case_insensitive(self, monkeypatch):
        """Header lookup should be case-insensitive."""
        received = []
        resp = _fake_response(
            status=200,
            headers={"x-capture-mode-requested": "FULL"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        t = Transport(
            base_url="http://localhost",
            api_key="key",
            on_capture_mode_requested=received.append,
        )
        t._do_upload(b"{}", None)

        assert received == ["FULL"]


# ---------------------------------------------------------------------------
# Client: logs when capture mode is requested via header
# ---------------------------------------------------------------------------


class TestClientCaptureModePropagation:
    def test_client_logs_capture_mode_requested(self, monkeypatch, caplog):
        """Client logs an info message when transport reports a capture mode request."""
        from incidentary.client import IncidentaryClient

        resp = _fake_response(
            status=200,
            headers={"X-Capture-Mode-Requested": "FULL"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        client = IncidentaryClient(
            api_key="test",
            service_name="svc",
            base_url="http://localhost:18080",
        )

        with caplog.at_level(logging.INFO, logger="incidentary.client"):
            # Call _do_upload directly to avoid threading
            client._transport._do_upload(b"{}", None)

        assert "capture mode requested" in caplog.text.lower()
        assert "FULL" in caplog.text

    def test_client_does_not_log_when_no_header(self, monkeypatch, caplog):
        """No log when backend returns no capture mode header."""
        from incidentary.client import IncidentaryClient

        resp = _fake_response(status=200, headers={})
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        client = IncidentaryClient(
            api_key="test",
            service_name="svc",
            base_url="http://localhost:18080",
        )

        with caplog.at_level(logging.INFO, logger="incidentary.client"):
            client._transport._do_upload(b"{}", None)

        assert "capture mode requested" not in caplog.text.lower()

    def test_transport_does_not_mutate_client_mode(self, monkeypatch):
        """Transport callback must NOT directly change the client's capture mode."""
        from incidentary.client import IncidentaryClient
        from incidentary.types import CaptureMode

        resp = _fake_response(
            status=200,
            headers={"X-Capture-Mode-Requested": "FULL"},
        )
        monkeypatch.setattr(
            "incidentary.transport.urllib.request.urlopen", lambda *a, **k: resp
        )

        client = IncidentaryClient(
            api_key="test",
            service_name="svc",
            base_url="http://localhost:18080",
        )
        original_mode = client.get_capture_mode()

        client._transport._do_upload(b"{}", None)

        # Transport must not have changed the client's capture mode
        assert client.get_capture_mode() == original_mode
        assert client.get_capture_mode() == CaptureMode.NORMAL
