"""Tests for the trackDeploy (fail-open) helper."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from incidentary.track_deploy import TrackDeployConfig, TrackDeployOptions, track_deploy


class _FakeResponse:
    def __init__(self, status: int = 202, body: bytes = b"{}"):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class _Capture:
    def __init__(self):
        self.url: str | None = None
        self.body: dict[str, Any] | None = None
        self.headers: dict[str, str] = {}
        self.method: str | None = None

    def __call__(self, request, *args, **kwargs):
        self.url = request.full_url
        self.method = request.get_method()
        raw = request.data.decode("utf-8") if request.data else ""
        self.body = json.loads(raw) if raw else None
        self.headers = {k.lower(): v for k, v in request.header_items()}
        return _FakeResponse()


def _cfg(**overrides) -> TrackDeployConfig:
    base = {"base_url": "https://api.incidentary.dev", "api_key": "ik_test_deadbeef"}
    base.update(overrides)
    return TrackDeployConfig(**base)


def _opts(**overrides) -> TrackDeployOptions:
    base: dict[str, Any] = {"service": "payments-api"}
    base.update(overrides)
    return TrackDeployOptions(**base)


class TestTrackDeployBodyShape:
    def test_posts_to_deploys_endpoint(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(
                _cfg(),
                _opts(
                    version="1.2.3",
                    commit_sha="abc1234",
                    commit_message="fix rounding",
                    branch="main",
                    deployed_by_name="Ada",
                    deployed_by_email="ada@example.com",
                    environment="staging",
                    diff_url="https://github.com/org/repo/compare/abc1234",
                    metadata={"pipeline": "ci-123"},
                ),
            )

        assert cap.url == "https://api.incidentary.dev/api/v1/deploys"
        assert cap.method == "POST"
        assert cap.body == {
            "service_name": "payments-api",
            "version": "1.2.3",
            "commit_sha": "abc1234",
            "commit_message": "fix rounding",
            "branch": "main",
            "deployed_by_name": "Ada",
            "deployed_by_email": "ada@example.com",
            "environment": "staging",
            "deploy_source": "sdk",
            "diff_url": "https://github.com/org/repo/compare/abc1234",
            "metadata": {"pipeline": "ci-123"},
            "deployed_at": cap.body["deployed_at"],  # verified separately below
        }

    def test_sets_authorization_and_content_type(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(api_key="ik_live_xyz"), _opts())

        assert cap.headers["authorization"] == "Bearer ik_live_xyz"
        assert cap.headers["content-type"] == "application/json"

    def test_deploy_source_is_always_sdk(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(), _opts())
        assert cap.body["deploy_source"] == "sdk"

    def test_environment_defaults_to_production(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(), _opts())
        assert cap.body["environment"] == "production"

    def test_deployed_at_defaults_to_now_iso8601(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(), _opts())
        # Must parse cleanly as ISO-8601 and be timezone-aware UTC.
        parsed = datetime.fromisoformat(cap.body["deployed_at"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    def test_deployed_at_custom_value_passes_through(self):
        cap = _Capture()
        fixed = datetime(2026, 4, 1, 9, 30, tzinfo=timezone.utc)
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(), _opts(deployed_at=fixed))
        assert cap.body["deployed_at"].startswith("2026-04-01T09:30:00")

    def test_omits_unset_optional_fields(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(), _opts())
        assert "version" not in cap.body
        assert "commit_sha" not in cap.body
        assert "commit_message" not in cap.body
        assert "branch" not in cap.body
        assert "deployed_by_name" not in cap.body
        assert "deployed_by_email" not in cap.body
        assert "diff_url" not in cap.body

    def test_trims_trailing_slash_from_base_url(self):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            track_deploy(_cfg(base_url="https://api.incidentary.dev/"), _opts())
        assert cap.url == "https://api.incidentary.dev/api/v1/deploys"
        # Guard against the specific regression where the post-host slash
        # gets doubled — "//api/v1" is the classic smell.
        assert "://api.incidentary.dev//" not in cap.url


class TestTrackDeployFailOpen:
    def test_network_error_does_not_raise(self, caplog: pytest.LogCaptureFixture):
        def boom(*_args, **_kwargs):
            raise urllib.error.URLError("connection refused")

        with patch("incidentary.track_deploy.urlopen", boom):
            with caplog.at_level(logging.WARNING):
                # Must not raise.
                track_deploy(_cfg(), _opts())

        messages = [r.getMessage() for r in caplog.records]
        assert any("track_deploy failed" in m for m in messages)

    def test_http_error_does_not_raise(self, caplog: pytest.LogCaptureFixture):
        def http_err(*_args, **_kwargs):
            return _FakeResponse(status=503, body=b'{"error":"busy"}')

        with patch("incidentary.track_deploy.urlopen", http_err):
            with caplog.at_level(logging.WARNING):
                track_deploy(_cfg(), _opts())

        messages = [r.getMessage() for r in caplog.records]
        assert any("503" in m for m in messages)

    def test_urllib_http_error_does_not_raise(self, caplog: pytest.LogCaptureFixture):
        def raise_http_err(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                url="http://example/api/v1/deploys",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=io.BytesIO(b"{}"),
            )

        with patch("incidentary.track_deploy.urlopen", raise_http_err):
            with caplog.at_level(logging.WARNING):
                track_deploy(_cfg(), _opts())

        messages = [r.getMessage() for r in caplog.records]
        assert any("400" in m for m in messages)

    def test_timeout_does_not_raise(self, caplog: pytest.LogCaptureFixture):
        import socket

        def timeout(*_args, **_kwargs):
            raise socket.timeout("timed out")

        with patch("incidentary.track_deploy.urlopen", timeout):
            with caplog.at_level(logging.WARNING):
                track_deploy(_cfg(), _opts())

        messages = [r.getMessage() for r in caplog.records]
        assert any("track_deploy failed" in m for m in messages)

    def test_empty_service_returns_early_without_network_call(
        self, caplog: pytest.LogCaptureFixture
    ):
        cap = _Capture()
        with patch("incidentary.track_deploy.urlopen", cap):
            with caplog.at_level(logging.WARNING):
                track_deploy(_cfg(), TrackDeployOptions(service=""))

        assert cap.url is None, "should not call the network when service is empty"
        messages = [r.getMessage() for r in caplog.records]
        assert any("service is required" in m for m in messages)
