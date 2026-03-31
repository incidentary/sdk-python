"""Flask integration — auto-wrap Flask.wsgi_app on every new Flask instance."""

from __future__ import annotations

import importlib.util
import logging
from typing import TYPE_CHECKING, Any, Optional

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations.flask")


class FlaskIntegration(Integration):
    """Instruments Flask by patching ``flask.Flask.__init__``.

    After patching, every new ``Flask`` instance automatically has its
    ``wsgi_app`` attribute wrapped with :class:`~incidentary.middleware.IncidentaryWSGIMiddleware`.
    This mirrors how OpenTelemetry instruments Flask.

    Skips patching if ``Flask.__init__`` already has ``__otel_original``
    (indicating OTel is present) to avoid double-wrapping.
    """

    def __init__(self) -> None:
        self._patched = False
        self._original_init: Any = None
        self._client: Optional[IncidentaryClient] = None

    @property
    def name(self) -> str:
        return "flask"

    def detect(self) -> bool:
        return importlib.util.find_spec("flask") is not None

    def patch(self, client: "IncidentaryClient") -> None:
        if self._patched:
            return
        try:
            import flask  # type: ignore[import-untyped]

            original_init = flask.Flask.__init__

            if hasattr(original_init, "__otel_original"):
                logger.warning(
                    "OpenTelemetry has already patched flask.Flask.__init__; "
                    "skipping Incidentary Flask patching."
                )
                return

            self._original_init = original_init
            self._client = client
            client_ref = client

            def _patched_init(self_flask: Any, *args: Any, **kwargs: Any) -> None:
                original_init(self_flask, *args, **kwargs)
                from ..middleware import IncidentaryWSGIMiddleware

                self_flask.wsgi_app = IncidentaryWSGIMiddleware(self_flask.wsgi_app, client_ref)

            flask.Flask.__init__ = _patched_init
            self._patched = True
        except Exception:
            logger.debug("Failed to patch flask.Flask.__init__", exc_info=True)

    def unpatch(self) -> None:
        if not self._patched:
            return
        try:
            import flask  # type: ignore[import-untyped]

            if self._original_init is not None:
                flask.Flask.__init__ = self._original_init
        except Exception:
            logger.debug("Failed to restore flask.Flask.__init__", exc_info=True)
        finally:
            self._original_init = None
            self._client = None
            self._patched = False

    def is_patched(self) -> bool:
        return self._patched
