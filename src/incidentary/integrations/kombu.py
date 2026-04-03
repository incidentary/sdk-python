"""kombu integration — trace propagation for AMQP producer/consumer."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.kombu")


class KombuIntegration(Integration):
    """Instruments kombu Producer.publish to inject trace headers.

    Patches ``kombu.Producer.publish`` so outbound messages carry
    ``x-incidentary-trace-id`` and ``x-incidentary-parent-ce`` headers.
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: IncidentaryClient | None = None
        self._original_publish: Any = None

    @property
    def name(self) -> str:
        return "kombu"

    def detect(self) -> bool:
        return importlib.util.find_spec("kombu") is not None

    def patch(self, client: IncidentaryClient) -> None:
        if self._patched:
            return
        try:
            import kombu  # type: ignore[import-untyped]

            original_publish = kombu.Producer.publish
            self._original_publish = original_publish
            self._client = client

            def _patched_publish(
                self_producer: Any,
                body: Any,
                routing_key: Any = None,
                **kwargs: Any,
            ) -> Any:
                try:
                    from ..context import get_trace_context

                    ctx = get_trace_context()
                    if ctx is not None:
                        # Copy kwargs to avoid mutating the caller's dict
                        existing = kwargs.get("headers") or {}
                        kwargs = dict(kwargs)
                        kwargs["headers"] = {
                            **existing,
                            "x-incidentary-trace-id": ctx.trace_id,
                            "x-incidentary-parent-ce": ctx.ce_id,
                        }
                except Exception:
                    pass

                start_ns = time.perf_counter_ns()
                try:
                    return original_publish(self_producer, body, routing_key=routing_key, **kwargs)
                finally:
                    try:
                        from ..context import get_trace_context
                        from ..types import RecordEventOptions

                        _client = _outer_client[0]
                        if _client is not None:
                            ctx = get_trace_context()
                            duration_ns = max(0, time.perf_counter_ns() - start_ns)
                            _client.record_event(
                                "queue_publish",
                                RecordEventOptions(
                                    trace_id=ctx.trace_id if ctx else None,
                                    parent_ce_id=ctx.ce_id if ctx else None,
                                    duration_ns=duration_ns,
                                ),
                            )
                    except Exception:
                        pass

            # Use a list cell so the closure captures a mutable reference
            # to the client even if it changes after patch().
            _outer_client: list[Any] = [client]

            # Store client reference in closure-accessible list
            _patched_publish._outer_client = _outer_client  # type: ignore[attr-defined]

            # Rebind to current client list so unpatch can update it
            self._client_cell = _outer_client

            kombu.Producer.publish = _patched_publish
            self._patched = True
        except Exception:
            logger.debug("Failed to patch kombu.Producer.publish", exc_info=True)

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            import kombu  # type: ignore[import-untyped]

            if self._original_publish is not None:
                kombu.Producer.publish = self._original_publish
        except Exception:
            logger.debug("Failed to restore kombu.Producer.publish", exc_info=True)
        finally:
            self._original_publish = None
            self._client = None
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched
