"""Core types for the Incidentary Python SDK."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Literal, Optional

RetryKeyQuality = Literal[
    "explicit",
    "route_template",
    "logical_edge",
    "normalized_url",
    "unknown",
]

PreArmTriggerType = Literal[
    "slow_success", "in_flight_pileup", "retry_onset", "error_rate_5xx"
]
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


class CaptureMode(str, Enum):
    NORMAL = "NORMAL"
    PRE_ARMED = "PRE_ARMED"
    INCIDENT = "INCIDENT"


class CeKind(str, Enum):
    HTTP_IN = "HTTP_IN"
    HTTP_OUT = "HTTP_OUT"
    QUEUE_PUBLISH = "QUEUE_PUBLISH"
    QUEUE_CONSUME = "QUEUE_CONSUME"
    INTERNAL = "INTERNAL"


TRACE_ID_HEADER = "x-incidentary-trace-id"
PARENT_CE_HEADER = "x-incidentary-parent-ce"


@dataclass
class CeDetail:
    method: Optional[str] = None
    route_key: Optional[str] = None
    route_template: Optional[str] = None
    request_bytes: Optional[int] = None
    response_bytes: Optional[int] = None
    request_headers: Optional[Dict[str, str]] = None
    response_headers: Optional[Dict[str, str]] = None
    retry: Optional[Dict[str, object]] = None
    downstream: Optional[Dict[str, object]] = None
    local_error_classification: Optional[Literal["none", "timeout", "cancelled"]] = None
    payload_snippet: Optional[str] = None


@dataclass
class SkeletonCe:
    ce_id: str
    trace_id: str
    parent_ce_id: Optional[str]
    service_id: str
    wall_ts_ns: int
    kind: str
    status: int
    duration_ns: int
    sdk_version: str = "0.2.0"
    captured_before_alert: Optional[bool] = None
    ring_buffer_seq: Optional[int] = None
    event_type: Optional[IncidentaryEventType] = None
    event_class: Optional[IncidentaryEventClass] = None
    event_attrs: Optional[Dict[str, Any]] = None
    detail: Optional[CeDetail] = None


@dataclass(frozen=True)
class RecordRequestOptions:
    kind: RequestKind = "HTTP_IN"
    duration_ns: int = 0
    cancelled: bool = False
    timed_out: bool = False
    outbound_retry_key_hash: int = 0
    outbound_retry_key_quality: RetryKeyQuality = "unknown"
    explicit_retry_observed: Optional[bool] = None


@dataclass(frozen=True)
class RecordEventOptions:
    trace_id: Optional[str] = None
    parent_ce_id: Optional[str] = None
    status: Optional[int] = None
    duration_ns: int = 0
    wall_ts_ns: Optional[int] = None
    event_attrs: Optional[Dict[str, Any]] = None


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
    details: Dict[str, float | int | str]


@dataclass
class PreArmWindow:
    id: str
    started_at_ms: int
    expires_at_ms: int
    reasons: list[PreArmTriggerReason]
    bound_incident_id: Optional[str]
    closed_at_ms: Optional[int]
    close_reason: Optional[
        Literal[
            "ttl", "error_rate_recovered", "incident_close", "manual", "state_reset"
        ]
    ]
