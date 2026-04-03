"""Non-blocking transport for the Python SDK.

Transport is fire-and-forget and fail-open by design.
It never raises into instrumented service code paths.
"""

from __future__ import annotations

import calendar
import json
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable, Iterable
from dataclasses import asdict, is_dataclass

from .types import SkeletonCe

SDK_VERSION = "0.2.0"
SCHEMA_VERSION = "1"


class Transport:
    """Fail-open background uploader. Never raises into caller."""

    def __init__(
        self,
        api_url: str | None = None,
        base_url: str | None = None,
        api_key: str = "",
        service_name: str = "",
        environment: str = "production",
        workspace_id: str = "",
        timeout_ms: int = 5000,
        on_error: Callable[[Exception], None] | None = None,
        circuit_breaker_cooldown_ms: int = 60_000,
    ):
        self.base_url = (base_url or api_url or "").strip().rstrip("/")
        self.api_key = api_key
        self.service_name = service_name
        self.environment = environment
        self.workspace_id = workspace_id
        self.timeout_ms = timeout_ms
        self.on_error = on_error
        self.circuit_breaker_cooldown_ms = circuit_breaker_cooldown_ms

        self._backend_healthy = True
        self._consecutive_failures = 0
        self._circuit_open_until_ms = 0
        self._quota_pause_until_ms = 0
        self._warned_missing_base_url = False
        self._lock = threading.Lock()

        if not self.base_url:
            self._warn_missing_base_url()

    @property
    def is_healthy(self) -> bool:
        if not self.base_url:
            return False
        if (
            self._quota_pause_until_ms != 0
            and int(time.time() * 1000) < self._quota_pause_until_ms
        ):
            return False
        return self._backend_healthy or int(time.time() * 1000) >= self._circuit_open_until_ms

    def upload_batch(
        self,
        events: Iterable[SkeletonCe],
        capture_mode: str = "SKELETON",
        incident_id: str | None = None,
    ) -> None:
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": self.workspace_id,
                "service_id": self.service_name,
                "environment": self.environment,
                "flushed_at": int(time.time() * 1_000_000_000),
                "capture_mode": capture_mode,
                "events": [
                    asdict(event) if is_dataclass(event) else event.__dict__ for event in events
                ],
                "sdk_telemetry": {
                    "sdk_version": SDK_VERSION,
                    "sdk_language": "python",
                    "queue_depth": 0,
                    "dropped_ce_count": 0,
                    "flush_latency_ms": 0,
                },
            }
            if not payload["events"] or not self._can_attempt_request():
                return
            body = json.dumps(payload).encode("utf-8")
        except Exception:
            return

        thread = threading.Thread(
            target=self._do_upload,
            args=(body, incident_id),
            daemon=True,
        )
        thread.start()

    def notify_backend(self, event: str, service_id: str, metadata: dict | None = None) -> None:
        if not self._can_attempt_request():
            return

        try:
            payload = {
                "service_id": service_id,
                "event": event,
                "metadata": metadata if metadata is not None else None,
            }
            body = json.dumps(payload).encode("utf-8")
        except Exception:
            return

        thread = threading.Thread(target=self._do_notify_backend, args=(body,), daemon=True)
        thread.start()

    def _do_upload(self, body: bytes, incident_id: str | None) -> None:
        if not self.base_url:
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-Incidentary-SDK-Version": SDK_VERSION,
        }
        if incident_id:
            headers["X-Incidentary-Incident-Id"] = incident_id

        req = urllib.request.Request(
            f"{self.base_url}/api/v1/ingest/batch",
            data=body,
            headers=headers,
            method="POST",
        )

        delays = [1, 4, 16]
        for attempt in range(len(delays) + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as response:
                    if 200 <= response.status < 300:
                        self._on_success()
                        return
            except urllib.error.HTTPError as err:
                body_bytes = err.read()
                if err.code == 426:
                    try:
                        data = json.loads(body_bytes.decode("utf-8"))
                    except Exception:
                        data = {}
                    print(
                        json.dumps(
                            {
                                "event": "incidentary_sdk_version_rejected",
                                "code": data.get("error", {}).get("code", "SDK_VERSION_TOO_OLD"),
                                "minimum_version": data.get("error", {}).get(
                                    "minimum_version", "unknown"
                                ),
                                "current_version": data.get("error", {}).get(
                                    "current_version", SDK_VERSION
                                ),
                            }
                        )
                    )
                    self._on_success()
                    return
                if err.code == 429:
                    try:
                        data = json.loads(body_bytes.decode("utf-8"))
                    except Exception:
                        data = {}
                    if self._pause_on_free_ce_limit(data):
                        return
                if attempt >= len(delays):
                    self._on_failure(
                        RuntimeError(f"Incidentary upload failed with HTTP {err.code}")
                    )
                    return
            except Exception as error:
                if attempt >= len(delays):
                    self._on_failure(_normalize_error(error))
                    return

            time.sleep(delays[attempt])

    def _do_notify_backend(self, body: bytes) -> None:
        if not self.base_url:
            return

        req = urllib.request.Request(
            f"{self.base_url}/api/v1/services/events",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as response:
                if 200 <= response.status < 300:
                    self._on_success()
                    return
            self._on_failure(RuntimeError("Incidentary service event upload failed"))
        except Exception as error:
            self._on_failure(_normalize_error(error))

    def _can_attempt_request(self) -> bool:
        now_ms = int(time.time() * 1000)
        if not self.base_url:
            self._warn_missing_base_url()
            return False

        with self._lock:
            if self._quota_pause_until_ms != 0 and now_ms < self._quota_pause_until_ms:
                return False
            if self._quota_pause_until_ms != 0 and now_ms >= self._quota_pause_until_ms:
                self._quota_pause_until_ms = 0
            if self._backend_healthy:
                return True
            if now_ms >= self._circuit_open_until_ms:
                self._backend_healthy = True
                self._consecutive_failures = 0
                return True
            return False

    def _on_success(self) -> None:
        with self._lock:
            self._backend_healthy = True
            self._consecutive_failures = 0
            self._circuit_open_until_ms = 0

    def _on_failure(self, error: Exception) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._backend_healthy = False
                self._circuit_open_until_ms = (
                    int(time.time() * 1000) + self.circuit_breaker_cooldown_ms
                )
        self._dispatch_error(error)

    def _dispatch_error(self, error: Exception) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(error)
        except Exception:
            return

    def _warn_missing_base_url(self) -> None:
        with self._lock:
            if self._warned_missing_base_url:
                return
            self._warned_missing_base_url = True
        error = RuntimeError(
            "Incidentary transport disabled because base_url is not configured. Pass base_url explicitly when constructing the SDK client."
        )
        warnings.warn(str(error), RuntimeWarning, stacklevel=2)
        self._dispatch_error(error)

    def _pause_on_free_ce_limit(self, payload: dict) -> bool:
        if (
            payload.get("error") != "ce_limit_reached"
            or payload.get("limit_type") != "ce"
            or payload.get("plan") != "free"
        ):
            return False

        reset_at_ms = _next_utc_month_start_ms()
        reset_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset_at_ms / 1000))

        with self._lock:
            self._quota_pause_until_ms = reset_at_ms

        print(
            json.dumps(
                {
                    "event": "incidentary_ce_limit_reached",
                    "plan": "free",
                    "limit": payload.get("limit"),
                    "reset_at": reset_at_iso,
                }
            )
        )
        self._dispatch_error(
            RuntimeError(
                f"Incidentary CE limit reached for the free plan. Pausing ingest until {reset_at_iso}."
            )
        )
        return True


def _normalize_error(error: Exception) -> Exception:
    return (
        error
        if isinstance(error, Exception)
        else RuntimeError("Incidentary transport request failed")
    )


def _next_utc_month_start_ms(now_s: float | None = None) -> int:
    current_s = time.time() if now_s is None else now_s
    now = time.gmtime(current_s)
    year = now.tm_year + (1 if now.tm_mon == 12 else 0)
    month = 1 if now.tm_mon == 12 else now.tm_mon + 1
    return int(calendar.timegm((year, month, 1, 0, 0, 0, 0, 0, 0)) * 1000)
