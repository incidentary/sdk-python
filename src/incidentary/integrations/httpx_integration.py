"""httpx integration — trace propagation for sync and async httpx transports."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.httpx")


class HttpxIntegration(Integration):
    """Instruments httpx HTTPTransport and AsyncHTTPTransport.

    Patches ``handle_request`` (sync) and ``handle_async_request`` (async) to
    inject trace headers and record ``http_out`` events.

    Skips patching if OpenTelemetry has already instrumented the methods
    (detected by ``__otel_original`` attribute).
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: IncidentaryClient | None = None
        self._original_handle_request: Any = None
        self._original_handle_async_request: Any = None

    @property
    def name(self) -> str:
        return "httpx"

    def detect(self) -> bool:
        return importlib.util.find_spec("httpx") is not None

    def patch(self, client: IncidentaryClient) -> None:
        if self._patched:
            return
        try:
            import httpx  # type: ignore[import-untyped]

            self._client = client
            self._patch_sync(httpx, client)
            self._patch_async(httpx, client)
            self._patched = True
        except Exception:
            logger.debug("Failed to patch httpx transports", exc_info=True)

    def _patch_sync(self, httpx: Any, client: IncidentaryClient) -> None:
        original = httpx.HTTPTransport.handle_request

        if hasattr(original, "__otel_original"):
            logger.warning(
                "OpenTelemetry has already patched httpx.HTTPTransport.handle_request; "
                "skipping Incidentary httpx sync patching."
            )
            return

        self._original_handle_request = original
        client_cell: list[Any] = [client]

        def _patched_handle_request(self_transport: Any, request: Any) -> Any:
            try:
                from ..context import get_trace_context
                from ..types import PARENT_CE_HEADER, TRACE_ID_HEADER

                ctx = get_trace_context()
                if ctx is not None:
                    try:
                        # Attempt direct mutation first (works for mutable
                        # headers and plain dicts).
                        request.headers[TRACE_ID_HEADER] = ctx.trace_id
                        request.headers[PARENT_CE_HEADER] = ctx.ce_id
                    except TypeError:
                        # httpx.Headers is immutable — build a new request
                        # with merged headers.
                        merged = dict(request.headers)
                        merged[TRACE_ID_HEADER] = ctx.trace_id
                        merged[PARENT_CE_HEADER] = ctx.ce_id
                        request = type(request)(
                            method=request.method,
                            url=request.url,
                            headers=merged,
                            stream=request.stream,
                        )
            except Exception:
                pass

            start_ns = time.perf_counter_ns()
            status_code = 0
            try:
                response = original(self_transport, request)
                status_code = int(getattr(response, "status_code", 0) or 0)
                return response
            except Exception:
                raise
            finally:
                _record_http_out(client_cell[0], start_ns, status_code)

        httpx.HTTPTransport.handle_request = _patched_handle_request

    def _patch_async(self, httpx: Any, client: IncidentaryClient) -> None:
        original = httpx.AsyncHTTPTransport.handle_async_request

        if hasattr(original, "__otel_original"):
            logger.warning(
                "OpenTelemetry has already patched httpx.AsyncHTTPTransport.handle_async_request; "
                "skipping Incidentary httpx async patching."
            )
            return

        self._original_handle_async_request = original
        client_cell: list[Any] = [client]

        async def _patched_handle_async_request(self_transport: Any, request: Any) -> Any:
            try:
                from ..context import get_trace_context
                from ..types import PARENT_CE_HEADER, TRACE_ID_HEADER

                ctx = get_trace_context()
                if ctx is not None:
                    try:
                        # Attempt direct mutation first (works for mutable
                        # headers and plain dicts).
                        request.headers[TRACE_ID_HEADER] = ctx.trace_id
                        request.headers[PARENT_CE_HEADER] = ctx.ce_id
                    except TypeError:
                        # httpx.Headers is immutable — build a new request
                        # with merged headers.
                        merged = dict(request.headers)
                        merged[TRACE_ID_HEADER] = ctx.trace_id
                        merged[PARENT_CE_HEADER] = ctx.ce_id
                        request = type(request)(
                            method=request.method,
                            url=request.url,
                            headers=merged,
                            stream=request.stream,
                        )
            except Exception:
                pass

            start_ns = time.perf_counter_ns()
            status_code = 0
            try:
                response = await original(self_transport, request)
                status_code = int(getattr(response, "status_code", 0) or 0)
                return response
            except Exception:
                raise
            finally:
                _record_http_out(client_cell[0], start_ns, status_code)

        httpx.AsyncHTTPTransport.handle_async_request = _patched_handle_async_request

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            import httpx  # type: ignore[import-untyped]

            if self._original_handle_request is not None:
                httpx.HTTPTransport.handle_request = self._original_handle_request
            if self._original_handle_async_request is not None:
                httpx.AsyncHTTPTransport.handle_async_request = self._original_handle_async_request
        except Exception:
            logger.debug("Failed to restore httpx transport methods", exc_info=True)
        finally:
            self._original_handle_request = None
            self._original_handle_async_request = None
            self._client = None
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched


def _record_http_out(client: Any, start_ns: int, status_code: int) -> None:
    """Record an http_out event. Never throws."""
    try:
        if client is None:
            return
        from ..context import get_trace_context
        from ..types import RecordEventOptions

        ctx = get_trace_context()
        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        client.record_event(
            "http_out",
            RecordEventOptions(
                trace_id=ctx.trace_id if ctx else None,
                parent_ce_id=ctx.ce_id if ctx else None,
                status=status_code if status_code > 0 else None,
                duration_ns=duration_ns,
            ),
        )
    except Exception:
        pass
