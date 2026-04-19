"""Record a deployment with Incidentary — fail-open by design.

The server uses these records to correlate deploys with incidents
that fire shortly after them. The call NEVER raises: if the request
fails for any reason we log a warning and return. A broken deploy
tracker must never break a deploy.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

_ENDPOINT_PATH = "/api/v1/deploys"
_DEFAULT_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger("incidentary.track_deploy")


@dataclass
class TrackDeployConfig:
    """Minimal transport config — kept independent of :class:`IncidentaryClient`
    so CI scripts can track a deploy without bootstrapping the full client."""

    base_url: str
    api_key: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass
class TrackDeployOptions:
    """Describes a single deploy event.

    Only `service` is required. Every other field is optional; unset
    optionals are omitted from the body rather than sent as ``null``.
    """

    service: str
    version: str | None = None
    commit_sha: str | None = None
    commit_message: str | None = None
    branch: str | None = None
    deployed_by_name: str | None = None
    deployed_by_email: str | None = None
    deployed_at: datetime | None = None
    environment: str | None = None
    diff_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def track_deploy(config: TrackDeployConfig, options: TrackDeployOptions) -> None:
    """POST a deploy event to Incidentary. Never raises."""
    if not options.service or not options.service.strip():
        logger.warning("incidentary.track_deploy: service is required — skipping")
        return

    url = config.base_url.rstrip("/") + _ENDPOINT_PATH
    body = _build_body(options)
    data = json.dumps(body).encode("utf-8")

    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            status = getattr(response, "status", 200)
            if status >= 400:
                logger.warning(
                    "incidentary.track_deploy failed: HTTP %d", status
                )
    except urllib.error.HTTPError as err:
        # Server returned a non-2xx status. Treat as fail-open.
        logger.warning("incidentary.track_deploy failed: HTTP %d", err.code)
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as err:
        # Network-level failure, timeout, or encoding issue.
        logger.warning("incidentary.track_deploy failed: %s", err)


def _build_body(options: TrackDeployOptions) -> dict[str, Any]:
    deployed_at = options.deployed_at or datetime.now(tz=timezone.utc)
    body: dict[str, Any] = {
        "service_name": options.service,
        "deploy_source": "sdk",
        "environment": options.environment or "production",
        "deployed_at": deployed_at.isoformat(),
        "metadata": options.metadata,
    }

    optional = {
        "version": options.version,
        "commit_sha": options.commit_sha,
        "commit_message": options.commit_message,
        "branch": options.branch,
        "deployed_by_name": options.deployed_by_name,
        "deployed_by_email": options.deployed_by_email,
        "diff_url": options.diff_url,
    }
    for key, value in optional.items():
        if value is not None:
            body[key] = value

    return body
