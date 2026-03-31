"""Incidentary integrations package."""

from __future__ import annotations

from .aiohttp_integration import AiohttpIntegration
from .asyncpg_integration import AsyncpgIntegration
from .base import Integration
from .celery import CeleryIntegration
from .django import DjangoIntegration
from .flask import FlaskIntegration
from .grpc_integration import GrpcIntegration
from .http import HTTPIntegration
from .httpx_integration import HttpxIntegration
from .kombu import KombuIntegration
from .psycopg2_integration import Psycopg2Integration
from .registry import IntegrationRegistry


def default_integrations() -> list[Integration]:
    """Return a fresh list of the default integrations.

    Each call returns new instances so that multiple clients do not share
    patching state.
    """
    return [
        HTTPIntegration(),
        CeleryIntegration(),
        KombuIntegration(),
        HttpxIntegration(),
        AiohttpIntegration(),
        DjangoIntegration(),
        FlaskIntegration(),
        Psycopg2Integration(),
        AsyncpgIntegration(),
        GrpcIntegration(),
    ]


__all__ = [
    "Integration",
    "HTTPIntegration",
    "CeleryIntegration",
    "KombuIntegration",
    "HttpxIntegration",
    "AiohttpIntegration",
    "DjangoIntegration",
    "FlaskIntegration",
    "Psycopg2Integration",
    "AsyncpgIntegration",
    "GrpcIntegration",
    "IntegrationRegistry",
    "default_integrations",
]
