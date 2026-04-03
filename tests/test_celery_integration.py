"""Tests for Celery integration (TDD — written before implementation).

All Celery library objects are mocked so celery does not need to be installed
in the test environment.
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


def _make_celery_signal_mock():
    """Return a mock that mimics a Celery Signal (connect / disconnect)."""
    signal = MagicMock()
    signal.connect = MagicMock()
    signal.disconnect = MagicMock()
    return signal


def _build_celery_signals_module():
    """Return a MagicMock representing the celery.signals module."""
    signals_mod = MagicMock()
    signals_mod.before_task_publish = _make_celery_signal_mock()
    signals_mod.task_prerun = _make_celery_signal_mock()
    signals_mod.task_postrun = _make_celery_signal_mock()
    signals_mod.task_failure = _make_celery_signal_mock()
    signals_mod.task_retry = _make_celery_signal_mock()
    return signals_mod


def _build_fake_celery():
    """Build a minimal fake celery package."""
    celery_mod = MagicMock()
    celery_mod.signals = _build_celery_signals_module()
    return celery_mod


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestCeleryDetect:
    def test_detect_returns_false_when_celery_not_installed(self):
        with patch.dict(sys.modules, {"celery": None}):
            import importlib
            import importlib.util

            with patch.object(importlib.util, "find_spec", return_value=None):
                from incidentary.integrations.celery import CeleryIntegration

                integration = CeleryIntegration()
                assert integration.detect() is False

    def test_detect_returns_true_when_celery_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        """detect() must use find_spec so it does not import celery."""
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("celery")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestCeleryName:
    def test_name_is_celery(self):
        from incidentary.integrations.celery import CeleryIntegration

        assert CeleryIntegration().name == "celery"


# ---------------------------------------------------------------------------
# is_patched / initial state
# ---------------------------------------------------------------------------


class TestCeleryIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.celery import CeleryIntegration

        integration = CeleryIntegration()
        assert integration.is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules, {"celery": fake_celery, "celery.signals": fake_celery.signals}
        ):
            from incidentary.integrations import celery as celery_mod

            importlib_reload_integration(celery_mod)
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules, {"celery": fake_celery, "celery.signals": fake_celery.signals}
        ):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


def importlib_reload_integration(mod):
    """Helper — no-op, integration objects are fresh per instantiation."""


# ---------------------------------------------------------------------------
# patch() — signal connections
# ---------------------------------------------------------------------------


class TestCeleryPatch:
    def setup_method(self):
        # Ensure clean state by importing fresh integration instances
        from incidentary.integrations.celery import CeleryIntegration

        self._cls = CeleryIntegration

    def test_patch_connects_before_task_publish_signal(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            integration = self._cls()
            client = _make_stub_client()
            integration.patch(client)

            fake_celery.signals.before_task_publish.connect.assert_called_once()

    def test_patch_connects_task_prerun_signal(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            integration = self._cls()
            client = _make_stub_client()
            integration.patch(client)

            fake_celery.signals.task_prerun.connect.assert_called_once()

    def test_patch_connects_task_postrun_signal(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            integration = self._cls()
            client = _make_stub_client()
            integration.patch(client)

            fake_celery.signals.task_postrun.connect.assert_called_once()

    def test_patch_is_idempotent(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            integration = self._cls()
            client = _make_stub_client()
            integration.patch(client)
            integration.patch(client)

            # Signals connected only once
            assert fake_celery.signals.before_task_publish.connect.call_count == 1

    def test_patch_does_not_raise_when_celery_missing(self):
        """patch() must silently do nothing if celery is absent."""
        with patch.dict(sys.modules, {"celery": None, "celery.signals": None}):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            # Must not raise
            integration.patch(client)


# ---------------------------------------------------------------------------
# unpatch() — signal disconnection
# ---------------------------------------------------------------------------


class TestCeleryUnpatch:
    def test_unpatch_disconnects_signals(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

            # At least before_task_publish should be disconnected
            fake_celery.signals.before_task_publish.disconnect.assert_called_once()

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.celery import CeleryIntegration

        integration = CeleryIntegration()
        # Must not raise
        integration.unpatch()

    def test_unpatch_is_idempotent(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()  # Second call must not raise


# ---------------------------------------------------------------------------
# Producer side — before_task_publish handler
# ---------------------------------------------------------------------------


class TestCeleryPublishHandler:
    def test_publish_injects_trace_headers_when_context_active(self):
        """The before_task_publish handler should inject trace context into headers."""
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)

            # Grab the handler that was connected to before_task_publish
            connect_call = fake_celery.signals.before_task_publish.connect.call_args
            handler = (
                connect_call[0][0]
                if connect_call[0]
                else connect_call[1].get("receiver") or connect_call[0][0]
            )

            set_trace_context("trace-pub-1", "ce-pub-1")
            headers = {}
            try:
                handler(sender="my_task", headers=headers)
            finally:
                clear_trace_context()

            assert headers.get("_incidentary_trace_id") == "trace-pub-1"
            assert headers.get("_incidentary_ce_id") == "ce-pub-1"

    def test_publish_does_not_inject_headers_when_no_context(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import clear_trace_context
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)

            connect_call = fake_celery.signals.before_task_publish.connect.call_args
            handler = connect_call[0][0]

            clear_trace_context()
            headers = {}
            handler(sender="my_task", headers=headers)

            assert "_incidentary_trace_id" not in headers
            assert "_incidentary_ce_id" not in headers

    def test_publish_records_queue_publish_event(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            integration.patch(client)

            connect_call = fake_celery.signals.before_task_publish.connect.call_args
            handler = connect_call[0][0]

            set_trace_context("trace-ev", "ce-ev")
            try:
                handler(sender="my_task", headers={})
            finally:
                clear_trace_context()

            client.record_event.assert_called_once()
            event_type = client.record_event.call_args[0][0]
            assert event_type == "queue_publish"

    def test_publish_handler_does_not_raise_on_exception(self):
        """Errors inside the handler must be swallowed."""
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            integration.patch(client)

            connect_call = fake_celery.signals.before_task_publish.connect.call_args
            handler = connect_call[0][0]

            # Must not raise even when record_event fails
            handler(sender="my_task", headers={})


# ---------------------------------------------------------------------------
# Consumer side — task_prerun / task_postrun handlers
# ---------------------------------------------------------------------------


class TestCeleryConsumeHandlers:
    def _get_handlers(self, fake_celery, integration, client):
        integration.patch(client)

        prerun_handler = fake_celery.signals.task_prerun.connect.call_args[0][0]
        postrun_handler = fake_celery.signals.task_postrun.connect.call_args[0][0]
        return prerun_handler, postrun_handler

    def test_prerun_extracts_trace_context_from_task_request(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import clear_trace_context, get_trace_context
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            prerun, _ = self._get_handlers(fake_celery, integration, client)

            clear_trace_context()

            task = MagicMock()
            task.request = {
                "_incidentary_trace_id": "t-consume",
                "_incidentary_ce_id": "c-consume",
            }

            prerun(sender="task_name", task_id="tid-1", task=task)

            ctx = get_trace_context()
            assert ctx is not None
            assert ctx.trace_id == "t-consume"
            assert ctx.ce_id == "c-consume"

            clear_trace_context()

    def test_prerun_does_not_set_context_when_headers_absent(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import clear_trace_context, get_trace_context
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            prerun, _ = self._get_handlers(fake_celery, integration, client)

            clear_trace_context()

            task = MagicMock()
            task.request = {}  # no incidentary headers

            prerun(sender="task_name", task_id="tid-2", task=task)

            ctx = get_trace_context()
            assert ctx is None

    def test_postrun_records_queue_consume_event(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            prerun, postrun = self._get_handlers(fake_celery, integration, client)

            set_trace_context("t-post", "c-post")
            task = MagicMock()
            task.request = {"_incidentary_trace_id": "t-post", "_incidentary_ce_id": "c-post"}

            prerun(sender="task_name", task_id="tid-3", task=task)

            try:
                postrun(
                    sender="task_name", task_id="tid-3", task=task, retval=None, state="SUCCESS"
                )
            finally:
                clear_trace_context()

            client.record_event.assert_called()
            # Find the queue_consume call
            consume_calls = [
                c for c in client.record_event.call_args_list if c[0][0] == "queue_consume"
            ]
            assert len(consume_calls) == 1

    def test_postrun_clears_trace_context(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.context import (
                get_trace_context,
                set_trace_context,
            )
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            prerun, postrun = self._get_handlers(fake_celery, integration, client)

            set_trace_context("t-clear", "c-clear")
            task = MagicMock()
            task.request = {"_incidentary_trace_id": "t-clear", "_incidentary_ce_id": "c-clear"}

            prerun(sender="task_name", task_id="tid-4", task=task)
            postrun(sender="task_name", task_id="tid-4", task=task, retval=None, state="SUCCESS")

            ctx = get_trace_context()
            assert ctx is None

    def test_postrun_handler_does_not_raise_on_exception(self):
        fake_celery = _build_fake_celery()
        with patch.dict(
            sys.modules,
            {"celery": fake_celery, "celery.signals": fake_celery.signals},
        ):
            from incidentary.integrations.celery import CeleryIntegration

            integration = CeleryIntegration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            prerun, postrun = self._get_handlers(fake_celery, integration, client)

            task = MagicMock()
            task.request = {"_incidentary_trace_id": "t-err", "_incidentary_ce_id": "c-err"}
            prerun(sender="n", task_id="t", task=task)
            # Must not raise
            postrun(sender="n", task_id="t", task=task, retval=None, state="SUCCESS")


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestCeleryABCConformance:
    def test_celery_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.celery import CeleryIntegration

        assert isinstance(CeleryIntegration(), Integration)

    def test_celery_integration_importable_from_integrations_package(self):
        from incidentary.integrations import CeleryIntegration

        assert CeleryIntegration is not None

    def test_default_integrations_includes_celery(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.celery import CeleryIntegration

        result = default_integrations()
        assert any(isinstance(i, CeleryIntegration) for i in result)
