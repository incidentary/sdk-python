"""Core types for the Incidentary Python SDK."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

RetryKeyQuality = Literal[
    "explicit",
    "route_template",
    "logical_edge",
    "normalized_url",
    "unknown",
]

PreArmTriggerType = Literal["slow_success", "in_flight_pileup", "retry_onset", "error_rate_5xx"]
PreArmTriggerSeverity = Literal["mild", "severe"]
RequestKind = Literal["HTTP_IN", "HTTP_OUT"]
IncidentaryEventType = Literal[
    "http_in",
    "http_out",
    "queue_publish",
    "queue_consume",
    "job_start",
    "job_end",
    "webhook_in",
    "webhook_out",
    "internal_task",
    "db_query",
    "grpc_in",
    "grpc_out",
]
IncidentaryEventClass = Literal["causal", "context"]


class CaptureMode(StrEnum):
    NORMAL = "NORMAL"
    PRE_ARMED = "PRE_ARMED"
    INCIDENT = "INCIDENT"


class CeKind(StrEnum):
    HTTP_IN = "HTTP_IN"
    HTTP_OUT = "HTTP_OUT"
    QUEUE_PUBLISH = "QUEUE_PUBLISH"
    QUEUE_CONSUME = "QUEUE_CONSUME"
    INTERNAL = "INTERNAL"


TRACE_ID_HEADER = "x-incidentary-trace-id"
PARENT_CE_HEADER = "x-incidentary-parent-ce"


@dataclass
class CeDetail:
    method: str | None = None
    route_key: str | None = None
    route_template: str | None = None
    request_bytes: int | None = None
    response_bytes: int | None = None
    request_headers: dict[str, str] | None = None
    response_headers: dict[str, str] | None = None
    retry: dict[str, object] | None = None
    downstream: dict[str, object] | None = None
    local_error_classification: Literal["none", "timeout", "cancelled"] | None = None
    payload_snippet: str | None = None


@dataclass
class SkeletonCe:
    ce_id: str
    trace_id: str
    parent_ce_id: str | None
    service_id: str
    wall_ts_ns: int
    kind: str
    status: int
    duration_ns: int
    sdk_version: str = "0.2.0"
    captured_before_alert: bool | None = None
    ring_buffer_seq: int | None = None
    event_type: IncidentaryEventType | None = None
    event_class: IncidentaryEventClass | None = None
    event_attrs: dict[str, Any] | None = None
    detail: CeDetail | None = None


@dataclass(frozen=True)
class RecordRequestOptions:
    kind: RequestKind = "HTTP_IN"
    duration_ns: int = 0
    cancelled: bool = False
    timed_out: bool = False
    outbound_retry_key_hash: int = 0
    outbound_retry_key_quality: RetryKeyQuality = "unknown"
    explicit_retry_observed: bool | None = None


@dataclass(frozen=True)
class RecordEventOptions:
    trace_id: str | None = None
    parent_ce_id: str | None = None
    status: int | None = None
    duration_ns: int = 0
    wall_ts_ns: int | None = None
    event_attrs: dict[str, Any] | None = None


@dataclass
class PreArmTriggerReason:
    trigger_type: PreArmTriggerType
    severity: PreArmTriggerSeverity
    observed_value: float
    threshold_value: float
    observed_label: str
    threshold_label: str
    fired_at_unix_ms: int
    summary: str
    details: dict[str, float | int | str]


@dataclass
class PreArmWindow:
    id: str
    started_at_ms: int
    expires_at_ms: int
    reasons: list[PreArmTriggerReason]
    bound_incident_id: str | None
    closed_at_ms: int | None
    close_reason: (
        Literal["ttl", "error_rate_recovered", "incident_close", "manual", "state_reset"] | None
    )
