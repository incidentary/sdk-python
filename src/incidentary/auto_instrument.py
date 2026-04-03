"""HTTP auto-instrumentation for urllib and requests.

Patches ``urllib.request.urlopen`` and ``requests.Session.send`` (when
available) so outbound HTTP calls automatically inject Incidentary trace
headers when a trace context is active.

All patching errors are swallowed — this module must never throw into
user code.
"""

from __future__ import annotations

import logging
import time
import urllib.request
from typing import TYPE_CHECKING, Any

from .context import get_trace_context
from .types import PARENT_CE_HEADER, TRACE_ID_HEADER, RecordEventOptions

if TYPE_CHECKING:
    from .client import IncidentaryClient

logger = logging.getLogger("incidentary.auto_instrument")

_patched = False
_originals: dict[str, Any] = {}
_client_ref: IncidentaryClient | None = None


def auto_instrument(client: IncidentaryClient) -> None:
    """Patch urllib and requests (if available) to inject trace headers.

    Idempotent — calling multiple times is safe and has no additional effect.
    """
    global _patched, _client_ref
    if _patched:
        return
    _client_ref = client
    _patch_urllib()
    _patch_requests()
    _patched = True


def undo_patches() -> None:
    """Restore original functions. Intended for testing."""
    global _patched, _client_ref

    if "urllib_urlopen" in _originals:
        urllib.request.urlopen = _originals["urllib_urlopen"]

    if "requests_session_send" in _originals:
        try:
            import requests

            requests.Session.send = _originals["requests_session_send"]
        except Exception:
            pass

    _originals.clear()
    _client_ref = None
    _patched = False


def is_patched() -> bool:
    """Return whether auto-instrumentation patches are currently active."""
    return _patched


# ---------------------------------------------------------------------------
# urllib patching
# ---------------------------------------------------------------------------


def _patch_urllib() -> None:
    """Monkey-patch ``urllib.request.urlopen`` to inject trace headers."""
    try:
        original = urllib.request.urlopen

        # OTel conflict detection
        if hasattr(original, "__otel_original"):
            logger.warning(
                "OpenTelemetry has already patched urllib.request.urlopen; "
                "skipping Incidentary urllib patching to avoid conflicts."
            )
            return

        _originals["urllib_urlopen"] = original

        def _instrumented_urlopen(url: Any, data: Any = None, *args: Any, **kwargs: Any) -> Any:
            try:
                ctx = get_trace_context()
                if ctx is not None:
                    # Convert string URL to Request for header injection
                    if isinstance(url, str):
                        url = urllib.request.Request(url)
                    url.add_header(TRACE_ID_HEADER, ctx.trace_id)
                    url.add_header(PARENT_CE_HEADER, ctx.ce_id)
            except Exception:
                pass

            start_ns = time.perf_counter_ns()
            status_code = 0
            try:
                response = original(url, data, *args, **kwargs)
                status_code = int(getattr(response, "status", 0) or 0)
                return response
            except Exception as exc:
                status_code = int(getattr(exc, "code", 0) or 0)
                raise
            finally:
                _record_http_out(start_ns, status_code)

        urllib.request.urlopen = _instrumented_urlopen  # type: ignore[assignment]
    except Exception:
        logger.debug("Failed to patch urllib.request.urlopen", exc_info=True)


# ---------------------------------------------------------------------------
# requests patching
# ---------------------------------------------------------------------------


def _patch_requests() -> None:
    """Monkey-patch ``requests.Session.send`` to inject trace headers.

    Gracefully skips if the ``requests`` library is not installed.
    """
    try:
        import requests
    except Exception:
        return

    try:
        original_send = requests.Session.send
        _originals["requests_session_send"] = original_send

        def _instrumented_send(self: Any, request: Any, **kwargs: Any) -> Any:
            try:
                ctx = get_trace_context()
                if ctx is not None:
                    request.headers[TRACE_ID_HEADER] = ctx.trace_id
                    request.headers[PARENT_CE_HEADER] = ctx.ce_id
            except Exception:
                pass

            start_ns = time.perf_counter_ns()
            status_code = 0
            try:
                response = original_send(self, request, **kwargs)
                status_code = int(getattr(response, "status_code", 0) or 0)
                return response
            except Exception as exc:
                status_code = int(getattr(exc, "code", 0) or 0)
                raise
            finally:
                _record_http_out(start_ns, status_code)

        requests.Session.send = _instrumented_send  # type: ignore[assignment]
    except Exception:
        logger.debug("Failed to patch requests.Session.send", exc_info=True)


# ---------------------------------------------------------------------------
# Event recording helper
# ---------------------------------------------------------------------------


def _record_http_out(start_ns: int, status_code: int) -> None:
    """Record an HTTP_OUT event on the client. Never throws."""
    try:
        client = _client_ref
        if client is None:
            return

        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        ctx = get_trace_context()

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
