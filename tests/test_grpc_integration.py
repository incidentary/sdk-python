"""Tests for gRPC integration (TDD — written before implementation).

grpc is mocked throughout; it does not need to be installed.
"""

from __future__ import annotations

import importlib.util
import sys
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


class MockClientCallDetails:
    def __init__(self, method="/test.Service/Method", metadata=None):
        self.method = method
        self.timeout = None
        self.metadata = metadata or []
        self.credentials = None
        self.wait_for_ready = None
        self.compression = None


class MockHandlerCallDetails:
    def __init__(self, metadata=None):
        self.invocation_metadata = metadata or []


# ---------------------------------------------------------------------------
# GrpcIntegration — detect()
# ---------------------------------------------------------------------------


class TestGrpcDetect:
    def test_detect_returns_false_when_grpc_not_installed(self):
        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.grpc_integration import GrpcIntegration

            integration = GrpcIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_grpc_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.grpc_integration import GrpcIntegration

            integration = GrpcIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.grpc_integration import GrpcIntegration

            integration = GrpcIntegration()
            integration.detect()
            mock_find_spec.assert_called_with("grpc")


# ---------------------------------------------------------------------------
# GrpcIntegration — patch() / unpatch() / is_patched()
# ---------------------------------------------------------------------------


class TestGrpcPatchLifecycle:
    def test_patch_stores_client_reference(self):
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integration = GrpcIntegration()
        client = _make_stub_client()
        integration.patch(client)
        assert integration._client is client

    def test_unpatch_clears_client_reference(self):
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integration = GrpcIntegration()
        client = _make_stub_client()
        integration.patch(client)
        integration.unpatch()
        assert integration._client is None

    def test_is_patched_false_before_patch(self):
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integration = GrpcIntegration()
        assert integration.is_patched() is False

    def test_is_patched_true_after_patch(self):
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integration = GrpcIntegration()
        integration.patch(_make_stub_client())
        assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integration = GrpcIntegration()
        integration.patch(_make_stub_client())
        integration.unpatch()
        assert integration.is_patched() is False

    def test_name_property(self):
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integration = GrpcIntegration()
        assert integration.name == "grpc"


# ---------------------------------------------------------------------------
# GrpcIntegration — interceptor factories
# ---------------------------------------------------------------------------


class TestGrpcInterceptorFactories:
    def test_client_interceptor_returns_instance(self):
        from incidentary.integrations.grpc_integration import (
            GrpcIntegration,
            IncidentaryClientInterceptor,
        )

        integration = GrpcIntegration()
        integration.patch(_make_stub_client())
        interceptor = integration.client_interceptor()
        assert isinstance(interceptor, IncidentaryClientInterceptor)

    def test_server_interceptor_returns_instance(self):
        from incidentary.integrations.grpc_integration import (
            GrpcIntegration,
            IncidentaryServerInterceptor,
        )

        integration = GrpcIntegration()
        integration.patch(_make_stub_client())
        interceptor = integration.server_interceptor()
        assert isinstance(interceptor, IncidentaryServerInterceptor)

    def test_client_interceptor_without_patch_still_returns_instance(self):
        from incidentary.integrations.grpc_integration import (
            GrpcIntegration,
            IncidentaryClientInterceptor,
        )

        integration = GrpcIntegration()
        interceptor = integration.client_interceptor()
        assert isinstance(interceptor, IncidentaryClientInterceptor)

    def test_server_interceptor_without_patch_still_returns_instance(self):
        from incidentary.integrations.grpc_integration import (
            GrpcIntegration,
            IncidentaryServerInterceptor,
        )

        integration = GrpcIntegration()
        interceptor = integration.server_interceptor()
        assert isinstance(interceptor, IncidentaryServerInterceptor)


# ---------------------------------------------------------------------------
# _ClientCallDetails
# ---------------------------------------------------------------------------


class TestClientCallDetails:
    def test_holds_all_fields_correctly(self):
        from incidentary.integrations.grpc_integration import _ClientCallDetails

        details = _ClientCallDetails(
            method="/pkg.Service/Method",
            timeout=5.0,
            metadata=[("key", "val")],
            credentials="creds",
            wait_for_ready=True,
            compression="gzip",
        )
        assert details.method == "/pkg.Service/Method"
        assert details.timeout == 5.0
        assert details.metadata == [("key", "val")]
        assert details.credentials == "creds"
        assert details.wait_for_ready is True
        assert details.compression == "gzip"


# ---------------------------------------------------------------------------
# IncidentaryClientInterceptor
# ---------------------------------------------------------------------------


class TestClientInterceptorHeaderInjection:
    def _make_interceptor(self, client=None):
        from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

        return IncidentaryClientInterceptor(client or _make_stub_client())

    def test_injects_trace_headers_when_context_present(self):
        from incidentary.context import clear_trace_context, set_trace_context
        from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

        set_trace_context("trace-abc", "ce-xyz")
        try:
            interceptor = self._make_interceptor()
            details = MockClientCallDetails(metadata=[])
            request = MagicMock()
            captured_details = []

            def continuation(call_details, req):
                captured_details.append(call_details)
                return MagicMock()

            interceptor.intercept_unary_unary(continuation, details, request)

            metadata_dict = dict(captured_details[0].metadata)
            assert metadata_dict[TRACE_ID_HEADER] == "trace-abc"
            assert metadata_dict[PARENT_CE_HEADER] == "ce-xyz"
        finally:
            clear_trace_context()

    def test_preserves_existing_metadata(self):
        from incidentary.context import clear_trace_context, set_trace_context

        set_trace_context("trace-123", "ce-456")
        try:
            interceptor = self._make_interceptor()
            details = MockClientCallDetails(metadata=[("x-custom", "custom-val")])
            request = MagicMock()
            captured_details = []

            def continuation(call_details, req):
                captured_details.append(call_details)
                return MagicMock()

            interceptor.intercept_unary_unary(continuation, details, request)

            metadata_dict = dict(captured_details[0].metadata)
            assert metadata_dict["x-custom"] == "custom-val"
        finally:
            clear_trace_context()

    def test_no_headers_injected_when_no_trace_context(self):
        from incidentary.context import clear_trace_context
        from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

        clear_trace_context()
        interceptor = self._make_interceptor()
        details = MockClientCallDetails(metadata=[])
        request = MagicMock()
        captured_details = []

        def continuation(call_details, req):
            captured_details.append(call_details)
            return MagicMock()

        interceptor.intercept_unary_unary(continuation, details, request)

        metadata_dict = dict(captured_details[0].metadata)
        assert TRACE_ID_HEADER not in metadata_dict
        assert PARENT_CE_HEADER not in metadata_dict

    def test_all_intercept_methods_call_continuation(self):
        from incidentary.context import clear_trace_context

        clear_trace_context()
        interceptor = self._make_interceptor()
        details = MockClientCallDetails()

        for method_name in (
            "intercept_unary_unary",
            "intercept_unary_stream",
            "intercept_stream_unary",
            "intercept_stream_stream",
        ):
            call_count = []

            def continuation(d, r, _count=call_count):
                _count.append(1)
                return MagicMock()

            method = getattr(interceptor, method_name)
            method(continuation, details, MagicMock())
            assert len(call_count) == 1, f"{method_name} did not call continuation"


class TestClientInterceptorEventRecording:
    def test_records_grpc_out_event_on_success(self):
        from incidentary.context import clear_trace_context, set_trace_context

        set_trace_context("trace-rec", "ce-rec")
        try:
            client = _make_stub_client()
            from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

            interceptor = IncidentaryClientInterceptor(client)
            details = MockClientCallDetails()

            def continuation(d, r):
                return MagicMock()

            interceptor.intercept_unary_unary(continuation, details, MagicMock())
            client.record_event.assert_called_once()
            event_type = client.record_event.call_args[0][0]
            assert event_type == "grpc_out"
        finally:
            clear_trace_context()

    def test_records_grpc_out_event_on_error(self):
        from incidentary.context import clear_trace_context

        clear_trace_context()
        client = _make_stub_client()
        from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

        interceptor = IncidentaryClientInterceptor(client)
        details = MockClientCallDetails()

        def continuation(d, r):
            raise RuntimeError("rpc failed")

        with pytest.raises(RuntimeError, match="rpc failed"):
            interceptor.intercept_unary_unary(continuation, details, MagicMock())

        client.record_event.assert_called_once()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "grpc_out"

    def test_nil_client_does_not_raise_in_record_event(self):
        from incidentary.context import clear_trace_context

        clear_trace_context()
        from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

        interceptor = IncidentaryClientInterceptor(None)
        details = MockClientCallDetails()

        def continuation(d, r):
            return MagicMock()

        # Must not raise
        interceptor.intercept_unary_unary(continuation, details, MagicMock())

    def test_error_in_continuation_is_reraised(self):
        from incidentary.context import clear_trace_context

        clear_trace_context()
        from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

        interceptor = IncidentaryClientInterceptor(None)
        details = MockClientCallDetails()

        class _SentinelError(Exception):
            pass

        def continuation(d, r):
            raise _SentinelError("boom")

        with pytest.raises(_SentinelError):
            interceptor.intercept_unary_unary(continuation, details, MagicMock())


class TestClientInterceptorNeverRaises:
    """The interceptor must never raise into user code from its own logic."""

    def test_broken_get_trace_context_does_not_raise(self):
        from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

        interceptor = IncidentaryClientInterceptor(None)
        details = MockClientCallDetails()

        with patch(
            "incidentary.integrations.grpc_integration.get_trace_context",
            side_effect=RuntimeError("ctx broken"),
        ):
            # Must not raise
            def continuation(d, r):
                return MagicMock()

            interceptor.intercept_unary_unary(continuation, details, MagicMock())

    def test_broken_record_event_does_not_raise(self):
        from incidentary.context import clear_trace_context

        clear_trace_context()
        client = _make_stub_client()
        client.record_event.side_effect = RuntimeError("record broken")
        from incidentary.integrations.grpc_integration import IncidentaryClientInterceptor

        interceptor = IncidentaryClientInterceptor(client)
        details = MockClientCallDetails()

        def continuation(d, r):
            return MagicMock()

        # Must not raise
        interceptor.intercept_unary_unary(continuation, details, MagicMock())


# ---------------------------------------------------------------------------
# IncidentaryServerInterceptor
# ---------------------------------------------------------------------------


class TestServerInterceptorContextExtraction:
    def _make_interceptor(self, client=None):
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        return IncidentaryServerInterceptor(client or _make_stub_client())

    def test_extracts_trace_context_from_metadata(self):
        from incidentary.context import clear_trace_context, get_trace_context
        from incidentary.types import PARENT_CE_HEADER, TRACE_ID_HEADER

        clear_trace_context()
        interceptor = self._make_interceptor()
        metadata = [(TRACE_ID_HEADER, "trace-srv"), (PARENT_CE_HEADER, "ce-srv")]
        handler_details = MockHandlerCallDetails(metadata=metadata)
        captured_context = []

        def continuation(details):
            captured_context.append(get_trace_context())
            return MagicMock()

        interceptor.intercept_service(continuation, handler_details)

        assert len(captured_context) == 1
        assert captured_context[0] is not None
        assert captured_context[0].trace_id == "trace-srv"
        assert captured_context[0].ce_id == "ce-srv"

    def test_sets_trace_context_during_handler(self):
        from incidentary.context import clear_trace_context, get_trace_context
        from incidentary.types import TRACE_ID_HEADER, PARENT_CE_HEADER

        clear_trace_context()
        interceptor = self._make_interceptor()
        metadata = [(TRACE_ID_HEADER, "trace-during"), (PARENT_CE_HEADER, "ce-during")]
        handler_details = MockHandlerCallDetails(metadata=metadata)

        context_inside = []

        def continuation(details):
            context_inside.append(get_trace_context())
            return MagicMock()

        interceptor.intercept_service(continuation, handler_details)
        assert context_inside[0].trace_id == "trace-during"

    def test_clears_trace_context_after_handler(self):
        from incidentary.context import clear_trace_context, get_trace_context
        from incidentary.types import TRACE_ID_HEADER, PARENT_CE_HEADER

        clear_trace_context()
        interceptor = self._make_interceptor()
        metadata = [(TRACE_ID_HEADER, "trace-clear"), (PARENT_CE_HEADER, "ce-clear")]
        handler_details = MockHandlerCallDetails(metadata=metadata)

        def continuation(details):
            return MagicMock()

        interceptor.intercept_service(continuation, handler_details)
        assert get_trace_context() is None

    def test_handles_missing_trace_headers_gracefully(self):
        from incidentary.context import clear_trace_context, get_trace_context

        clear_trace_context()
        interceptor = self._make_interceptor()
        handler_details = MockHandlerCallDetails(metadata=[])

        called = []

        def continuation(details):
            called.append(True)
            return MagicMock()

        interceptor.intercept_service(continuation, handler_details)
        assert called, "continuation must be called even without trace headers"

    def test_clears_context_after_handler_even_on_exception(self):
        from incidentary.context import clear_trace_context, get_trace_context
        from incidentary.types import TRACE_ID_HEADER, PARENT_CE_HEADER

        clear_trace_context()
        interceptor = self._make_interceptor()
        metadata = [(TRACE_ID_HEADER, "trace-ex"), (PARENT_CE_HEADER, "ce-ex")]
        handler_details = MockHandlerCallDetails(metadata=metadata)

        def continuation(details):
            raise RuntimeError("handler error")

        with pytest.raises(RuntimeError):
            interceptor.intercept_service(continuation, handler_details)

        assert get_trace_context() is None


class TestServerInterceptorEventRecording:
    def test_records_grpc_in_event(self):
        from incidentary.context import clear_trace_context
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        clear_trace_context()
        client = _make_stub_client()
        interceptor = IncidentaryServerInterceptor(client)
        handler_details = MockHandlerCallDetails()

        def continuation(details):
            return MagicMock()

        interceptor.intercept_service(continuation, handler_details)
        client.record_event.assert_called_once()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "grpc_in"

    def test_nil_client_does_not_raise(self):
        from incidentary.context import clear_trace_context
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        clear_trace_context()
        interceptor = IncidentaryServerInterceptor(None)
        handler_details = MockHandlerCallDetails()

        def continuation(details):
            return MagicMock()

        # Must not raise
        interceptor.intercept_service(continuation, handler_details)

    def test_records_grpc_in_event_on_handler_error(self):
        from incidentary.context import clear_trace_context
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        clear_trace_context()
        client = _make_stub_client()
        interceptor = IncidentaryServerInterceptor(client)
        handler_details = MockHandlerCallDetails()

        def continuation(details):
            raise RuntimeError("handler exploded")

        with pytest.raises(RuntimeError):
            interceptor.intercept_service(continuation, handler_details)

        client.record_event.assert_called_once()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "grpc_in"


class TestServerInterceptorNeverRaises:
    def test_broken_set_trace_context_does_not_raise(self):
        from incidentary.context import clear_trace_context
        from incidentary.types import TRACE_ID_HEADER, PARENT_CE_HEADER
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        clear_trace_context()
        interceptor = IncidentaryServerInterceptor(None)
        metadata = [(TRACE_ID_HEADER, "trace-broken"), (PARENT_CE_HEADER, "ce-broken")]
        handler_details = MockHandlerCallDetails(metadata=metadata)

        with patch(
            "incidentary.integrations.grpc_integration.set_trace_context",
            side_effect=RuntimeError("ctx broken"),
        ):
            called = []

            def continuation(details):
                called.append(True)
                return MagicMock()

            # Must not raise
            interceptor.intercept_service(continuation, handler_details)
            assert called

    def test_broken_clear_trace_context_does_not_raise(self):
        from incidentary.context import clear_trace_context
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        clear_trace_context()
        interceptor = IncidentaryServerInterceptor(None)
        handler_details = MockHandlerCallDetails()

        with patch(
            "incidentary.integrations.grpc_integration.clear_trace_context",
            side_effect=RuntimeError("clear broken"),
        ):
            def continuation(details):
                return MagicMock()

            # Must not raise
            interceptor.intercept_service(continuation, handler_details)

    def test_broken_record_event_does_not_raise(self):
        from incidentary.context import clear_trace_context
        from incidentary.integrations.grpc_integration import IncidentaryServerInterceptor

        clear_trace_context()
        client = _make_stub_client()
        client.record_event.side_effect = RuntimeError("record broken")
        interceptor = IncidentaryServerInterceptor(client)
        handler_details = MockHandlerCallDetails()

        def continuation(details):
            return MagicMock()

        # Must not raise
        interceptor.intercept_service(continuation, handler_details)


# ---------------------------------------------------------------------------
# Integration export checks
# ---------------------------------------------------------------------------


class TestExports:
    def test_grpc_integration_in_integrations_init(self):
        from incidentary.integrations import GrpcIntegration

        assert GrpcIntegration is not None

    def test_grpc_integration_in_top_level_init(self):
        from incidentary import GrpcIntegration

        assert GrpcIntegration is not None

    def test_grpc_integration_in_default_integrations(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.grpc_integration import GrpcIntegration

        integrations = default_integrations()
        types = [type(i) for i in integrations]
        assert GrpcIntegration in types
