"""Base class for Incidentary integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import IncidentaryClient


class Integration(ABC):
    """Abstract base for all Incidentary integrations.

    Concrete subclasses must implement all abstract methods and the ``name``
    property.  The contract is intentionally minimal so that adding new
    integrations in the future requires no changes to the registry.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short, unique identifier for this integration (e.g. ``"http"``)."""
        ...

    @abstractmethod
    def detect(self) -> bool:
        """Return True if the library or environment is present and patchable."""
        ...

    @abstractmethod
    def patch(self, client: IncidentaryClient) -> None:
        """Apply instrumentation patches, storing a reference to *client*."""
        ...

    @abstractmethod
    def unpatch(self) -> None:
        """Restore original functions and release any client reference."""
        ...

    @abstractmethod
    def is_patched(self) -> bool:
        """Return True if patches are currently active."""
        ...
