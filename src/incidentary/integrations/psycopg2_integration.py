"""psycopg2 integration — instrument cursor.execute and cursor.executemany."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.psycopg2")


class Psycopg2Integration(Integration):
    """Instruments psycopg2 by patching ``cursor.execute`` and ``cursor.executemany``.

    Records ``db_query`` events with ``kind='INTERNAL'`` on every query.
    Statement text is truncated to 500 characters before recording.
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: Optional[IncidentaryClient] = None
        self._original_execute: Any = None
        self._original_executemany: Any = None

    @property
    def name(self) -> str:
        return "psycopg2"

    def detect(self) -> bool:
        return importlib.util.find_spec("psycopg2") is not None

    def patch(self, client: "IncidentaryClient") -> None:
        if self._patched:
            return
        try:
            import psycopg2.extensions  # type: ignore[import-untyped]

            self._original_execute = psycopg2.extensions.cursor.execute
            self._original_executemany = psycopg2.extensions.cursor.executemany
            self._client = client

            original_execute = self._original_execute
            original_executemany = self._original_executemany

            def _patched_execute(cursor_self: Any, query: Any, vars: Any = None) -> Any:
                start_ns = time.perf_counter_ns()
                try:
                    result = original_execute(cursor_self, query, vars)
                    _record_db_query(client, start_ns, "execute", _safe_statement(query))
                    return result
                except Exception:
                    _record_db_query(
                        client, start_ns, "execute", _safe_statement(query), error=True
                    )
                    raise

            def _patched_executemany(
                cursor_self: Any, query: Any, vars_list: Any
            ) -> Any:
                start_ns = time.perf_counter_ns()
                try:
                    result = original_executemany(cursor_self, query, vars_list)
                    _record_db_query(client, start_ns, "executemany", _safe_statement(query))
                    return result
                except Exception:
                    _record_db_query(
                        client, start_ns, "executemany", _safe_statement(query), error=True
                    )
                    raise

            psycopg2.extensions.cursor.execute = _patched_execute
            psycopg2.extensions.cursor.executemany = _patched_executemany
            self._patched = True
        except Exception:
            logger.debug("Failed to patch psycopg2 cursor methods", exc_info=True)

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            import psycopg2.extensions  # type: ignore[import-untyped]

            if self._original_execute is not None:
                psycopg2.extensions.cursor.execute = self._original_execute
            if self._original_executemany is not None:
                psycopg2.extensions.cursor.executemany = self._original_executemany
        except Exception:
            logger.debug("Failed to restore psycopg2 cursor methods", exc_info=True)
        finally:
            self._original_execute = None
            self._original_executemany = None
            self._client = None
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched


def _safe_statement(query: Any, max_len: int = 500) -> str:
    """Truncate and sanitize a query string for safe recording.

    Returns an empty string for non-string inputs.
    """
    if not isinstance(query, str):
        return ""
    return query[:max_len]


def _record_db_query(
    client: Any,
    start_ns: int,
    operation: str,
    statement: str,
    error: bool = False,
) -> None:
    """Record an INTERNAL/db_query event. Never raises."""
    try:
        if client is None:
            return
        from ..context import get_trace_context
        from ..types import RecordEventOptions

        ctx = get_trace_context()
        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        client.record_event(
            "db_query",
            RecordEventOptions(
                trace_id=ctx.trace_id if ctx else None,
                parent_ce_id=ctx.ce_id if ctx else None,
                duration_ns=duration_ns,
                status=500 if error else 0,
                event_attrs={"kind": "INTERNAL", "operation": operation, "statement": statement},
            ),
        )
    except Exception:
        pass
