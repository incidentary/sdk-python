"""Celery integration — trace propagation via Celery signals."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.celery")

_MAX_TRACKED_TASKS = 10_000


class CeleryIntegration(Integration):
    """Instruments Celery tasks using built-in signals.

    Producer side: connects to ``before_task_publish`` to inject trace context
    into task headers.

    Consumer side: connects to ``task_prerun`` / ``task_postrun`` to extract
    context and record ``queue_consume`` events.

    No monkey-patching — Celery's signal system is used exclusively.
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: IncidentaryClient | None = None
        # Keep references to bound handlers so we can disconnect them.
        self._on_publish: Any = None
        self._on_prerun: Any = None
        self._on_postrun: Any = None
        # Per-task start times keyed by task_id.
        self._task_start_ns: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "celery"

    def detect(self) -> bool:
        return importlib.util.find_spec("celery") is not None

    def patch(self, client: IncidentaryClient) -> None:
        if self._patched:
            return
        try:
            from celery import signals as _signals  # type: ignore[import-untyped]

            self._client = client

            def _on_publish(sender: Any, headers: Any, **kwargs: Any) -> None:
                try:
                    from ..context import get_trace_context

                    ctx = get_trace_context()
                    if ctx is not None:
                        headers["_incidentary_trace_id"] = ctx.trace_id
                        headers["_incidentary_ce_id"] = ctx.ce_id
                    _client = self._client
                    if _client is not None:
                        from ..types import RecordEventOptions

                        _client.record_event(
                            "queue_publish",
                            RecordEventOptions(
                                trace_id=ctx.trace_id if ctx else None,
                                parent_ce_id=ctx.ce_id if ctx else None,
                            ),
                        )
                except Exception:
                    pass

            def _on_prerun(sender: Any, task_id: Any, task: Any, **kwargs: Any) -> None:
                try:
                    from ..context import set_trace_context

                    request = task.request
                    trace_id = (
                        request.get("_incidentary_trace_id") if isinstance(request, dict) else None
                    )
                    ce_id = (
                        request.get("_incidentary_ce_id") if isinstance(request, dict) else None
                    )
                    if trace_id:
                        set_trace_context(trace_id, ce_id or "")
                    # Cap dict size to prevent unbounded memory growth
                    if len(self._task_start_ns) >= _MAX_TRACKED_TASKS:
                        oldest_key = next(iter(self._task_start_ns))
                        del self._task_start_ns[oldest_key]
                    self._task_start_ns[str(task_id)] = time.perf_counter_ns()
                except Exception:
                    pass

            def _on_postrun(sender: Any, task_id: Any, task: Any, **kwargs: Any) -> None:
                try:
                    from ..context import clear_trace_context, get_trace_context
                    from ..types import RecordEventOptions

                    ctx = get_trace_context()
                    start_ns = self._task_start_ns.pop(str(task_id), None)
                    duration_ns = (
                        max(0, time.perf_counter_ns() - start_ns) if start_ns is not None else 0
                    )
                    _client = self._client
                    if _client is not None:
                        _client.record_event(
                            "queue_consume",
                            RecordEventOptions(
                                trace_id=ctx.trace_id if ctx else None,
                                parent_ce_id=ctx.ce_id if ctx else None,
                                duration_ns=duration_ns,
                            ),
                        )
                except Exception:
                    pass
                try:
                    from ..context import clear_trace_context

                    clear_trace_context()
                except Exception:
                    pass

            self._on_publish = _on_publish
            self._on_prerun = _on_prerun
            self._on_postrun = _on_postrun

            _signals.before_task_publish.connect(_on_publish)
            _signals.task_prerun.connect(_on_prerun)
            _signals.task_postrun.connect(_on_postrun)

            self._patched = True
        except Exception:
            logger.debug("Failed to patch Celery signals", exc_info=True)

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            from celery import signals as _signals  # type: ignore[import-untyped]

            if self._on_publish is not None:
                _signals.before_task_publish.disconnect(self._on_publish)
            if self._on_prerun is not None:
                _signals.task_prerun.disconnect(self._on_prerun)
            if self._on_postrun is not None:
                _signals.task_postrun.disconnect(self._on_postrun)
        except Exception:
            logger.debug("Failed to disconnect Celery signals", exc_info=True)
        finally:
            self._on_publish = None
            self._on_prerun = None
            self._on_postrun = None
            self._client = None
            self._task_start_ns.clear()
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched
