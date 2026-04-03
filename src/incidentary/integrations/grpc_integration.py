"""gRPC integration — trace propagation via gRPC interceptors.

Provides client and server interceptors that inject and extract
x-incidentary-* headers through gRPC metadata, without monkey-patching.
"""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from ..context import clear_trace_context, get_trace_context, set_trace_context
from ..types import PARENT_CE_HEADER, TRACE_ID_HEADER, RecordEventOptions
from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.grpc")


class GrpcIntegration(Integration):
    """gRPC integration that provides client and server interceptors.

    Unlike monkey-patching integrations, gRPC has a clean interceptor API.
    This integration does not patch any library code; instead it exposes
    ``client_interceptor()`` and ``server_interceptor()`` factory methods
    that users pass directly to grpc channel / server construction.

    Usage (client)::

        channel = grpc.intercept_channel(
            grpc.insecure_channel('localhost:50051'),
            grpc_integration.client_interceptor(),
        )

    Usage (server)::

        server = grpc.server(
            futures.ThreadPoolExecutor(),
            interceptors=[grpc_integration.server_interceptor()],
        )
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: IncidentaryClient | None = None

    @property
    def name(self) -> str:
        return "grpc"

    def detect(self) -> bool:
        return importlib.util.find_spec("grpc") is not None

    def patch(self, client: IncidentaryClient) -> None:
        self._client = client
        self._patched = True

    def unpatch(self) -> None:
        self._client = None
        self._patched = False

    def is_patched(self) -> bool:
        return self._patched

    def client_interceptor(self) -> IncidentaryClientInterceptor:
        """Return a gRPC client interceptor for outbound calls."""
        return IncidentaryClientInterceptor(self._client)

    def server_interceptor(self) -> IncidentaryServerInterceptor:
        """Return a gRPC server interceptor for inbound calls."""
        return IncidentaryServerInterceptor(self._client)


class _ClientCallDetails:
    """Immutable replacement for grpc.ClientCallDetails with updated metadata."""

    __slots__ = (
        "compression",
        "credentials",
        "metadata",
        "method",
        "timeout",
        "wait_for_ready",
    )

    def __init__(
        self,
        method: Any,
        timeout: Any,
        metadata: Any,
        credentials: Any,
        wait_for_ready: Any,
        compression: Any,
    ) -> None:
        self.method = method
        self.timeout = timeout
        self.metadata = metadata
        self.credentials = credentials
        self.wait_for_ready = wait_for_ready
        self.compression = compression


class IncidentaryClientInterceptor:
    """gRPC client interceptor that injects trace context into outgoing metadata."""

    def __init__(self, client: IncidentaryClient | None) -> None:
        self._client = client

    def intercept_unary_unary(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        return self._intercept(continuation, client_call_details, request)

    def intercept_unary_stream(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        return self._intercept(continuation, client_call_details, request)

    def intercept_stream_unary(
        self, continuation: Any, client_call_details: Any, request_iterator: Any
    ) -> Any:
        return self._intercept(continuation, client_call_details, request_iterator)

    def intercept_stream_stream(
        self, continuation: Any, client_call_details: Any, request_iterator: Any
    ) -> Any:
        return self._intercept(continuation, client_call_details, request_iterator)

    def _intercept(
        self,
        continuation: Any,
        client_call_details: Any,
        request_or_iterator: Any,
    ) -> Any:
        """Inject trace context into metadata and record a grpc_out event."""
        try:
            ctx = get_trace_context()
            if ctx is not None:
                metadata = list(client_call_details.metadata or [])
                metadata.append((TRACE_ID_HEADER, ctx.trace_id))
                metadata.append((PARENT_CE_HEADER, ctx.ce_id))
                client_call_details = _ClientCallDetails(
                    method=client_call_details.method,
                    timeout=client_call_details.timeout,
                    metadata=metadata,
                    credentials=client_call_details.credentials,
                    wait_for_ready=client_call_details.wait_for_ready,
                    compression=client_call_details.compression,
                )
        except Exception:
            pass

        start_ns = time.perf_counter_ns()
        try:
            response = continuation(client_call_details, request_or_iterator)
            self._record_event(start_ns, "grpc_out", 0)
            return response
        except Exception:
            self._record_event(start_ns, "grpc_out", 500)
            raise

    def _record_event(self, start_ns: int, event_type: str, status: int) -> None:
        try:
            if self._client is None:
                return
            duration_ns = max(0, time.perf_counter_ns() - start_ns)
            ctx = get_trace_context()
            self._client.record_event(
                event_type,
                RecordEventOptions(
                    trace_id=ctx.trace_id if ctx else None,
                    parent_ce_id=ctx.ce_id if ctx else None,
                    status=status if status else None,
                    duration_ns=duration_ns,
                ),
            )
        except Exception:
            pass


class IncidentaryServerInterceptor:
    """gRPC server interceptor that extracts trace context from incoming metadata."""

    def __init__(self, client: IncidentaryClient | None) -> None:
        self._client = client

    def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        """Extract trace context from metadata, wrap handler for per-call context."""
        trace_id = ""
        ce_id = ""
        try:
            metadata = dict(handler_call_details.invocation_metadata or [])
            trace_id = metadata.get(TRACE_ID_HEADER, "")
            ce_id = metadata.get(PARENT_CE_HEADER, "")
        except Exception:
            pass

        # Set context so it's available during continuation (handler lookup).
        if trace_id:
            try:
                set_trace_context(trace_id, ce_id)
            except Exception:
                pass

        start_ns = time.perf_counter_ns()
        try:
            handler = continuation(handler_call_details)
            self._record_event(start_ns, "grpc_in", 0, trace_id, ce_id)
            return handler
        except Exception:
            self._record_event(start_ns, "grpc_in", 500, trace_id, ce_id)
            raise
        finally:
            try:
                clear_trace_context()
            except Exception:
                pass

    def _record_event(
        self,
        start_ns: int,
        event_type: str,
        status: int,
        trace_id: str = "",
        ce_id: str = "",
    ) -> None:
        try:
            if self._client is None:
                return
            duration_ns = max(0, time.perf_counter_ns() - start_ns)
            ctx = get_trace_context()
            self._client.record_event(
                event_type,
                RecordEventOptions(
                    trace_id=ctx.trace_id if ctx else (trace_id or None),
                    parent_ce_id=ctx.ce_id if ctx else (ce_id or None),
                    status=status if status else None,
                    duration_ns=duration_ns,
                ),
            )
        except Exception:
            pass
