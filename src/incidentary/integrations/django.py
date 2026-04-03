"""Django integration — auto-inject Incidentary middleware into MIDDLEWARE list."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.django")

_MIDDLEWARE_PATH = "incidentary.integrations.django.IncidentaryDjangoMiddleware"

# Module-level client reference accessed by the middleware class.
_client: IncidentaryClient | None = None


def _set_client(client: IncidentaryClient) -> None:
    global _client
    _client = client


def _get_client() -> IncidentaryClient | None:
    return _client


class DjangoIntegration(Integration):
    """Instruments Django by injecting Incidentary middleware at position 0.

    Works by inserting :class:`IncidentaryDjangoMiddleware` into
    ``django.conf.settings.MIDDLEWARE`` at startup.  If Django is not installed
    or settings are not yet configured the patch is silently skipped.
    """

    def __init__(self) -> None:
        self._patched = False

    @property
    def name(self) -> str:
        return "django"

    def detect(self) -> bool:
        return importlib.util.find_spec("django") is not None

    def patch(self, client: IncidentaryClient) -> None:
        if self._patched:
            return
        try:
            from django.conf import settings  # type: ignore[import-untyped]

            if not settings.configured:
                return

            if _MIDDLEWARE_PATH in settings.MIDDLEWARE:
                return

            _set_client(client)
            settings.MIDDLEWARE.insert(0, _MIDDLEWARE_PATH)
            self._patched = True
        except Exception:
            logger.debug("Failed to inject Django middleware", exc_info=True)

    def unpatch(self) -> None:
        try:
            from django.conf import settings  # type: ignore[import-untyped]

            if _MIDDLEWARE_PATH in settings.MIDDLEWARE:
                settings.MIDDLEWARE.remove(_MIDDLEWARE_PATH)
        except Exception:
            logger.debug("Failed to remove Django middleware", exc_info=True)
        finally:
            _set_client(None)
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched


class IncidentaryDjangoMiddleware:
    """Django middleware that wraps each request with trace context.

    Extracts or generates a trace ID, sets context, records an ``http_in``
    event, and clears context in a ``finally`` block so it is always cleaned up.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response
        # Capture client reference at middleware chain-build time,
        # not per-request, to avoid depending on the module-level global
        # during every request.
        self._client = _get_client()

    def __call__(self, request: Any) -> Any:
        trace_id = request.META.get("HTTP_X_INCIDENTARY_TRACE_ID") or str(uuid4())
        ce_id = str(uuid4())

        from ..context import clear_trace_context, set_trace_context

        set_trace_context(trace_id, ce_id)
        start_ns = time.perf_counter_ns()
        status_code = 200

        try:
            response = self.get_response(request)
            status_code = getattr(response, "status_code", 200)
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            clear_trace_context()
            _record_http_in(start_ns, status_code, trace_id, ce_id)


def _record_http_in(
    start_ns: int,
    status_code: int,
    trace_id: str | None,
    ce_id: str | None,
) -> None:
    """Record an http_in event. Never raises."""
    try:
        client = _get_client()
        if client is None:
            return
        from ..types import RecordEventOptions

        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        client.record_event(
            "http_in",
            RecordEventOptions(
                trace_id=trace_id,
                parent_ce_id=ce_id,
                status=status_code,
                duration_ns=duration_ns,
            ),
        )
    except Exception:
        pass
