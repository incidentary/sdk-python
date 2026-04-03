"""asyncpg integration — instrument Connection.execute/fetch/fetchval/fetchrow."""

from __future__ import annotations

import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from .base import Integration
from .psycopg2_integration import _safe_statement

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.asyncpg")

_PATCHED_METHODS = ("execute", "fetch", "fetchval", "fetchrow")


class AsyncpgIntegration(Integration):
    """Instruments asyncpg by patching ``Connection.execute``, ``fetch``,
    ``fetchval``, and ``fetchrow``.

    Records ``db_query`` events with ``kind='INTERNAL'`` on every query.
    Statement text is truncated to 500 characters before recording.
    """

    def __init__(self) -> None:
        self._patched = False
        self._client: IncidentaryClient | None = None
        self._originals: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "asyncpg"

    def detect(self) -> bool:
        return importlib.util.find_spec("asyncpg") is not None

    def patch(self, client: IncidentaryClient) -> None:
        if self._patched:
            return
        try:
            import asyncpg  # type: ignore[import-untyped]

            self._client = client

            for method_name in _PATCHED_METHODS:
                original = getattr(asyncpg.Connection, method_name)
                self._originals[method_name] = original

                # Use default argument capture to bind original and name into
                # the coroutine's closure at definition time.
                async def _patched(
                    conn_self: Any,
                    query: Any,
                    *args: Any,
                    _orig: Any = original,
                    _name: str = method_name,
                    **kwargs: Any,
                ) -> Any:
                    start_ns = time.perf_counter_ns()
                    try:
                        result = await _orig(conn_self, query, *args, **kwargs)
                        _record_db_query(client, start_ns, _name, _safe_statement(query))
                        return result
                    except Exception:
                        _record_db_query(
                            client, start_ns, _name, _safe_statement(query), error=True
                        )
                        raise

                setattr(asyncpg.Connection, method_name, _patched)

            self._patched = True
        except Exception:
            logger.debug("Failed to patch asyncpg Connection methods", exc_info=True)

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            import asyncpg  # type: ignore[import-untyped]

            for method_name, original in self._originals.items():
                setattr(asyncpg.Connection, method_name, original)
        except Exception:
            logger.debug("Failed to restore asyncpg Connection methods", exc_info=True)
        finally:
            self._originals.clear()
            self._client = None
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched


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
