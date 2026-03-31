"""aiohttp integration — trace propagation for aiohttp ClientSession."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.aiohttp")


class AiohttpIntegration(Integration):
    """Instruments aiohttp ClientSession._request to inject trace headers.

    Patches the private ``_request`` coroutine on ``ClientSession`` so that
    every outbound request automatically carries ``x-incidentary-trace-id``
    and ``x-incidentary-parent-ce`` headers when a trace context is active.
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: IncidentaryClient | None = None
        self._original_request: Any = None

    @property
    def name(self) -> str:
        return "aiohttp"

    def detect(self) -> bool:
        return importlib.util.find_spec("aiohttp") is not None

    def patch(self, client: "IncidentaryClient") -> None:
        if self._patched:
            return
        try:
            import aiohttp  # type: ignore[import-untyped]

            original = aiohttp.ClientSession._request
            self._original_request = original
            self._client = client
            client_cell: list[Any] = [client]

            async def _patched_request(
                self_session: Any, method: Any, url: Any, **kwargs: Any
            ) -> Any:
                try:
                    from ..context import get_trace_context
                    from ..types import PARENT_CE_HEADER, TRACE_ID_HEADER

                    ctx = get_trace_context()
                    if ctx is not None:
                        # Copy kwargs to avoid mutating the caller's dict
                        existing = kwargs.get("headers") or {}
                        kwargs = dict(kwargs)
                        kwargs["headers"] = {
                            **existing,
                            TRACE_ID_HEADER: ctx.trace_id,
                            PARENT_CE_HEADER: ctx.ce_id,
                        }
                except Exception:
                    pass

                start_ns = time.perf_counter_ns()
                status_code = 0
                try:
                    response = await original(self_session, method, url, **kwargs)
                    status_code = int(getattr(response, "status", 0) or 0)
                    return response
                except Exception:
                    raise
                finally:
                    _record_http_out(client_cell[0], start_ns, status_code)

            aiohttp.ClientSession._request = _patched_request
            self._patched = True
        except Exception:
            logger.debug("Failed to patch aiohttp.ClientSession._request", exc_info=True)

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            import aiohttp  # type: ignore[import-untyped]

            if self._original_request is not None:
                aiohttp.ClientSession._request = self._original_request
        except Exception:
            logger.debug("Failed to restore aiohttp.ClientSession._request", exc_info=True)
        finally:
            self._original_request = None
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
