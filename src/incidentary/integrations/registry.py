"""Integration registry — discovers and manages Incidentary integrations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import Integration

if TYPE_CHECKING:
    from ..client import IncidentaryClient

logger = logging.getLogger("incidentary.integrations")


class IntegrationRegistry:
    """Manages a collection of :class:`Integration` instances.

    The registry is deliberately defensive: every operation is wrapped in
    try/except so that a misbehaving integration can never raise into user
    code.
    """

    def __init__(self) -> None:
        self._integrations: list[Integration] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, integration: Integration) -> None:
        """Add *integration* to the registry."""
        self._integrations.append(integration)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def registered(self) -> list[Integration]:
        """Return a copy of all registered integrations."""
        return list(self._integrations)

    @property
    def active(self) -> list[Integration]:
        """Return a copy of currently-patched integrations."""
        result: list[Integration] = []
        for integration in self._integrations:
            try:
                if integration.is_patched():
                    result.append(integration)
            except Exception:
                logger.debug(
                    "Integration %r raised in is_patched()",
                    getattr(integration, "name", repr(integration)),
                    exc_info=True,
                )
        return result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def discover_and_patch(self, client: IncidentaryClient) -> None:
        """For each registered integration, call detect(); patch if True.

        Exceptions from either :meth:`detect` or :meth:`patch` are caught
        and logged so that a faulty integration cannot break client startup.
        """
        for integration in self._integrations:
            name = getattr(integration, "name", repr(integration))
            try:
                detected = integration.detect()
            except Exception:
                logger.debug(
                    "Integration %r raised in detect(); skipping",
                    name,
                    exc_info=True,
                )
                continue

            if not detected:
                logger.debug("Integration %r not detected; skipping", name)
                continue

            try:
                integration.patch(client)
                logger.debug("Integration %r patched successfully", name)
            except Exception:
                logger.debug(
                    "Integration %r raised in patch(); skipping",
                    name,
                    exc_info=True,
                )

    def unpatch_all(self) -> None:
        """Call :meth:`unpatch` on every currently-active integration.

        Exceptions from :meth:`unpatch` are caught and logged individually
        so that one failing integration cannot prevent others from being
        cleaned up.
        """
        for integration in self._integrations:
            name = getattr(integration, "name", repr(integration))
            try:
                if not integration.is_patched():
                    continue
            except Exception:
                logger.debug(
                    "Integration %r raised in is_patched() during unpatch_all(); skipping",
                    name,
                    exc_info=True,
                )
                continue

            try:
                integration.unpatch()
                logger.debug("Integration %r unpatched successfully", name)
            except Exception:
                logger.debug(
                    "Integration %r raised in unpatch()",
                    name,
                    exc_info=True,
                )
