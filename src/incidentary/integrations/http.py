"""HTTP integration — wraps the existing auto_instrument patching logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient


class HTTPIntegration(Integration):
    """Instruments urllib and requests (when available) for trace propagation.

    This is a thin wrapper around :mod:`incidentary.auto_instrument` that
    adapts it to the :class:`~incidentary.integrations.base.Integration`
    interface.  All state (patched/unpatched, client reference) lives in
    the underlying module-level globals so that the existing
    :func:`~incidentary.auto_instrument.auto_instrument`,
    :func:`~incidentary.auto_instrument.undo_patches`, and
    :func:`~incidentary.auto_instrument.is_patched` public API remains
    unaffected.
    """

    @property
    def name(self) -> str:
        return "http"

    def detect(self) -> bool:
        """urllib is part of the stdlib — always available."""
        return True

    def patch(self, client: IncidentaryClient) -> None:
        from ..auto_instrument import auto_instrument as _auto_instrument

        _auto_instrument(client)

    def unpatch(self) -> None:
        from ..auto_instrument import undo_patches as _undo_patches

        _undo_patches()

    def is_patched(self) -> bool:
        from ..auto_instrument import is_patched as _is_patched

        return _is_patched()
