"""Tests for kombu/RabbitMQ integration (TDD — written before implementation).

kombu is mocked throughout; it does not need to be installed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


def _build_fake_kombu():
    """Build a minimal fake kombu package with Producer and Consumer."""
    kombu_mod = MagicMock()

    class FakeProducer:
        def publish(self, body, routing_key=None, **kwargs):
            return None

    class FakeConsumer:
        def receive(self, body, message):
            return None

    kombu_mod.Producer = FakeProducer
    kombu_mod.Consumer = FakeConsumer
    return kombu_mod


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestKombuDetect:
    def test_detect_returns_false_when_kombu_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_kombu_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("kombu")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestKombuName:
    def test_name_is_kombu(self):
        from incidentary.integrations.kombu import KombuIntegration

        assert KombuIntegration().name == "kombu"


# ---------------------------------------------------------------------------
# is_patched
# ---------------------------------------------------------------------------


class TestKombuIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.kombu import KombuIntegration

        integration = KombuIntegration()
        assert integration.is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_kombu = _build_fake_kombu()
        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_kombu = _build_fake_kombu()
        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — monkey-patching Producer.publish
# ---------------------------------------------------------------------------


class TestKombuPatch:
    def test_patch_replaces_producer_publish(self):
        fake_kombu = _build_fake_kombu()
        original_publish = fake_kombu.Producer.publish
        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)

            assert fake_kombu.Producer.publish is not original_publish

    def test_patch_is_idempotent(self):
        fake_kombu = _build_fake_kombu()
        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)
            patched_once = fake_kombu.Producer.publish
            integration.patch(client)
            patched_twice = fake_kombu.Producer.publish
            assert patched_once is patched_twice

    def test_patch_does_not_raise_when_kombu_missing(self):
        with patch.dict(sys.modules, {"kombu": None}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            # Must not raise
            integration.patch(client)


# ---------------------------------------------------------------------------
# unpatch()
# ---------------------------------------------------------------------------


class TestKombuUnpatch:
    def test_unpatch_restores_original_publish(self):
        fake_kombu = _build_fake_kombu()
        original_publish = fake_kombu.Producer.publish
        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

            assert fake_kombu.Producer.publish is original_publish

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.kombu import KombuIntegration

        integration = KombuIntegration()
        # Must not raise
        integration.unpatch()

    def test_unpatch_is_idempotent(self):
        fake_kombu = _build_fake_kombu()
        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()  # Must not raise


# ---------------------------------------------------------------------------
# Producer patching behaviour
# ---------------------------------------------------------------------------


class TestKombuProducerPatch:
    def test_publish_injects_trace_id_into_headers(self):
        fake_kombu = _build_fake_kombu()
        captured = {}

        def recording_publish(self, body, routing_key=None, **kwargs):
            captured.update(kwargs)

        fake_kombu.Producer.publish = recording_publish

        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)

            set_trace_context("trace-kombu-1", "ce-kombu-1")
            try:
                producer_instance = fake_kombu.Producer()
                fake_kombu.Producer.publish(producer_instance, body={"data": 1})
            finally:
                clear_trace_context()

        headers = captured.get("headers", {})
        assert headers.get("x-incidentary-trace-id") == "trace-kombu-1"
        assert headers.get("x-incidentary-parent-ce") == "ce-kombu-1"

    def test_publish_does_not_inject_headers_when_no_context(self):
        fake_kombu = _build_fake_kombu()
        captured = {}

        def recording_publish(self, body, routing_key=None, **kwargs):
            captured.update(kwargs)

        fake_kombu.Producer.publish = recording_publish

        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.context import clear_trace_context
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)

            clear_trace_context()
            producer_instance = fake_kombu.Producer()
            fake_kombu.Producer.publish(producer_instance, body={"data": 1})

        headers = captured.get("headers", {})
        assert "x-incidentary-trace-id" not in headers
        assert "x-incidentary-parent-ce" not in headers

    def test_publish_records_queue_publish_event(self):
        fake_kombu = _build_fake_kombu()

        def noop_publish(self, body, routing_key=None, **kwargs):
            pass

        fake_kombu.Producer.publish = noop_publish

        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)

            set_trace_context("trace-kombu-ev", "ce-kombu-ev")
            try:
                producer_instance = fake_kombu.Producer()
                fake_kombu.Producer.publish(producer_instance, body={"data": 1})
            finally:
                clear_trace_context()

        client.record_event.assert_called_once()
        assert client.record_event.call_args[0][0] == "queue_publish"

    def test_publish_patch_calls_through_to_original(self):
        """Patched publish must still call the original."""
        fake_kombu = _build_fake_kombu()
        call_log = []

        def original_publish(self, body, routing_key=None, **kwargs):
            call_log.append(body)

        fake_kombu.Producer.publish = original_publish

        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            integration.patch(client)

            producer_instance = fake_kombu.Producer()
            fake_kombu.Producer.publish(producer_instance, body={"key": "val"})

        assert {"key": "val"} in call_log

    def test_publish_does_not_raise_on_record_event_failure(self):
        fake_kombu = _build_fake_kombu()

        def noop_publish(self, body, routing_key=None, **kwargs):
            pass

        fake_kombu.Producer.publish = noop_publish

        with patch.dict(sys.modules, {"kombu": fake_kombu}):
            from incidentary.integrations.kombu import KombuIntegration

            integration = KombuIntegration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            integration.patch(client)

            producer_instance = fake_kombu.Producer()
            # Must not raise
            fake_kombu.Producer.publish(producer_instance, body={})


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestKombuABCConformance:
    def test_kombu_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.kombu import KombuIntegration

        assert isinstance(KombuIntegration(), Integration)

    def test_kombu_integration_importable_from_integrations_package(self):
        from incidentary.integrations import KombuIntegration

        assert KombuIntegration is not None

    def test_default_integrations_includes_kombu(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.kombu import KombuIntegration

        result = default_integrations()
        assert any(isinstance(i, KombuIntegration) for i in result)
