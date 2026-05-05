"""WSGI/ASGI middleware for Incidentary Python SDK."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .client import IncidentaryClient
from .context import clear_trace_context, set_trace_context
from .downstream_edge_key import DownstreamEdgeKeyResolution, DownstreamEdgeKeyResolver
from .types import (
    PARENT_CE_HEADER,
    TRACE_ID_HEADER,
    CeDetail,
    CeKind,
    RecordRequestOptions,
    SkeletonCe,
)

resolver = DownstreamEdgeKeyResolver()


def extract_trace_context(headers: Mapping[str, str | list[str] | None]) -> tuple[str, str | None]:
    """Extract trace_id and parent_ce_id from request headers."""
    lower = {
        str(k).lower(): str(v)
        for k, v in headers.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    trace_id = lower.get(TRACE_ID_HEADER) or str(uuid.uuid4())
    parent_ce = lower.get(PARENT_CE_HEADER) or None
    return trace_id, parent_ce


def inject_trace_context(headers: dict[str, str], trace_id: str, ce_id: str) -> None:
    """Inject causal headers into an outbound request headers dict."""
    headers[TRACE_ID_HEADER] = trace_id
    headers[PARENT_CE_HEADER] = ce_id


class IncidentaryWSGIMiddleware:
    """
    WSGI middleware. Wrap your app:
        app = IncidentaryWSGIMiddleware(app, client)
    """

    def __init__(self, app: Callable, client: IncidentaryClient):
        self._app = app
        self._client = client

    def __call__(self, environ: dict, start_response: Callable) -> object:
        headers = {
            key[5:].lower().replace("_", "-"): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }

        trace_id, parent_ce = extract_trace_context(headers)
        ce_id = str(uuid.uuid4())
        start_ns = time.perf_counter_ns()

        environ["incidentary.trace_id"] = trace_id
        environ["incidentary.ce_id"] = ce_id

        set_trace_context(trace_id, ce_id)

        captured_status = [200]
        captured_headers: list[tuple[str, str]] = []

        self._client.record_request_start("HTTP_IN")

        def capturing_start_response(status: str, headers_list: list, exc_info=None):
            captured_status[0] = _parse_status_code(status)
            captured_headers.clear()
            for header in headers_list:
                if isinstance(header, tuple) and len(header) >= 2:
                    captured_headers.append((str(header[0]), str(header[1])))
            return start_response(status, headers_list, exc_info)

        try:
            result = self._app(environ, capturing_start_response)
        finally:
            clear_trace_context()

        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        status_code = captured_status[0]

        self._client.record_request(
            status_code,
            RecordRequestOptions(
                kind="HTTP_IN",
                duration_ns=duration_ns,
                cancelled=False,
                timed_out=False,
            ),
        )

        ce_base = SkeletonCe(
            id=ce_id,
            trace_id=trace_id,
            parent_id=parent_ce,
            service_id=self._client.service_name,
            occurred_at=int(time.time() * 1_000_000_000),
            kind=CeKind.HTTP_SERVER.value,
            event_type="http_server",
            status_code=status_code,
            duration_ns=duration_ns,
        )
        detail = _build_inbound_detail(self._client, environ, headers, captured_headers)
        ce = self._client.attach_detail_to_event(ce_base, detail)
        self._client.write_event(ce)

        return result


class IncidentaryASGIMiddleware:
    """
    ASGI middleware. Wrap your app:
        app = IncidentaryASGIMiddleware(app, client)

    Only instruments HTTP scopes; lifespan and websocket pass through.
    """

    def __init__(self, app: Any, client: Any):
        self._app = app
        self._client = client

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or []
        header_map: dict[str, str] = {}
        for name_bytes, value_bytes in raw_headers:
            header_map[name_bytes.decode("latin-1").lower()] = value_bytes.decode("latin-1")

        trace_id, parent_ce = extract_trace_context(header_map)
        ce_id = str(uuid.uuid4())
        start_ns = time.perf_counter_ns()

        set_trace_context(trace_id, ce_id)

        captured_status = [200]
        self._client.record_request_start("HTTP_IN")

        async def capturing_send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                captured_status[0] = int(message.get("status", 200))
            await send(message)

        try:
            await self._app(scope, receive, capturing_send)
        finally:
            clear_trace_context()

        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        status_code = captured_status[0]

        self._client.record_request(
            status_code,
            RecordRequestOptions(
                kind="HTTP_IN",
                duration_ns=duration_ns,
                cancelled=False,
                timed_out=False,
            ),
        )

        ce_base = SkeletonCe(
            id=ce_id,
            trace_id=trace_id,
            parent_id=parent_ce,
            service_id=self._client.service_name,
            occurred_at=int(time.time() * 1_000_000_000),
            kind=CeKind.HTTP_SERVER.value,
            event_type="http_server",
            status_code=status_code,
            duration_ns=duration_ns,
        )
        self._client.write_event(ce_base)


def instrumented_urlopen(
    client: IncidentaryClient,
    parent_context: dict[str, str] | None,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | bytearray | None = None,
    timeout: float | None = None,
    retry_metadata: Mapping[str, object] | None = None,
):
    """Instrument outbound urllib call and emit HTTP_OUT CE + trigger signals."""
    trace_id = (parent_context or {}).get("trace_id") or str(uuid.uuid4())
    parent_ce = (parent_context or {}).get("ce_id")
    ce_id = str(uuid.uuid4())

    request_headers = dict(headers or {})
    inject_trace_context(request_headers, trace_id, ce_id)

    normalized_method = (method or "GET").upper()
    edge = resolver.resolve(
        trace_id=trace_id, method=normalized_method, url=url, metadata=retry_metadata
    )
    explicit_retry_observed = _extract_explicit_retry_observed(retry_metadata)
    retry_key_hash = _hash_retry_identity(edge.key_for_hash)

    request = urllib.request.Request(
        url=url, data=data, headers=request_headers, method=normalized_method
    )

    status_code = 0
    timed_out = False
    cancelled = False
    response_headers: dict[str, str] = {}
    start_ns = time.perf_counter_ns()

    client.record_request_start("HTTP_OUT")

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        status_code = int(getattr(response, "status", 0) or 0)
        if getattr(response, "headers", None) is not None:
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        return response
    except urllib.error.HTTPError as err:
        status_code = int(getattr(err, "code", 0) or 0)
        if getattr(err, "headers", None) is not None:
            response_headers = {str(k).lower(): str(v) for k, v in err.headers.items()}
        raise
    except Exception as err:
        status_code = 0
        timed_out = isinstance(err, TimeoutError)
        cancelled = timed_out
        raise
    finally:
        duration_ns = max(0, time.perf_counter_ns() - start_ns)
        client.record_request(
            status_code,
            RecordRequestOptions(
                kind="HTTP_OUT",
                duration_ns=duration_ns,
                cancelled=cancelled,
                timed_out=timed_out,
                outbound_retry_key_hash=retry_key_hash,
                outbound_retry_key_quality=edge.key_quality,
                explicit_retry_observed=explicit_retry_observed,
            ),
        )

        ce_base = SkeletonCe(
            id=ce_id,
            trace_id=trace_id,
            parent_id=parent_ce,
            service_id=client.service_name,
            occurred_at=int(time.time() * 1_000_000_000),
            kind=CeKind.HTTP_CLIENT.value,
            event_type="http_client",
            status_code=status_code,
            duration_ns=duration_ns,
        )
        detail = _build_outbound_detail(
            client,
            request_headers,
            response_headers,
            normalized_method,
            edge,
            retry_metadata,
            explicit_retry_observed,
            data,
            cancelled,
            timed_out,
        )
        ce = client.attach_detail_to_event(ce_base, detail)
        client.write_event(ce)


def _build_inbound_detail(
    client: IncidentaryClient,
    environ: Mapping[str, object],
    request_headers: Mapping[str, str],
    response_headers: list[tuple[str, str]],
) -> CeDetail | None:
    if not client.should_capture_detail_for_current_mode():
        return None

    method = str(environ.get("REQUEST_METHOD") or "GET").upper()
    route_input = str(environ.get("PATH_INFO") or environ.get("REQUEST_URI") or "/")
    route_template = _optional_str(environ.get("incidentary.route_template"))

    route = resolver.resolve(trace_id="local", method=method, url=route_input)

    response_headers_map = {name.lower(): value for name, value in response_headers}

    return CeDetail(
        method=method,
        route_key=route.route_key,
        route_template=route_template,
        request_bytes=_parse_content_length(_optional_str(environ.get("CONTENT_LENGTH"))),
        response_bytes=_parse_content_length(response_headers_map.get("content-length")),
        request_headers=_filter_headers(
            request_headers, client.get_detail_request_header_allowlist()
        ),
        response_headers=_filter_headers(
            response_headers_map, client.get_detail_response_header_allowlist()
        ),
        local_error_classification="none",
    )


def _build_outbound_detail(
    client: IncidentaryClient,
    request_headers: Mapping[str, str],
    response_headers: Mapping[str, str],
    method: str,
    edge: DownstreamEdgeKeyResolution,
    retry_metadata: Mapping[str, object] | None,
    explicit_retry_observed: bool | None,
    request_body: bytes | bytearray | None,
    cancelled: bool,
    timed_out: bool,
) -> CeDetail | None:
    if not client.should_capture_detail_for_current_mode():
        return None

    request_bytes = (
        len(request_body)
        if request_body is not None
        else _parse_content_length(request_headers.get("content-length"))
    )

    return CeDetail(
        method=method,
        route_key=edge.route_key,
        route_template=_optional_str(
            (retry_metadata or {}).get("route_template") if retry_metadata else None
        ),
        request_bytes=request_bytes,
        response_bytes=_parse_content_length(response_headers.get("content-length")),
        request_headers=_filter_headers(
            request_headers, client.get_detail_request_header_allowlist()
        ),
        response_headers=_filter_headers(
            response_headers, client.get_detail_response_header_allowlist()
        ),
        retry={
            "explicit_observed": explicit_retry_observed,
            "key_quality": edge.key_quality,
            "edge_key": edge.edge_key,
            "operation_key": edge.operation_key,
        },
        downstream={
            "edge_key": edge.edge_key,
            "service": _optional_str(
                (retry_metadata or {}).get("downstream_service") if retry_metadata else None
            ),
            "operation_name": _optional_str(
                (retry_metadata or {}).get("operation_name") if retry_metadata else None
            ),
            "key_quality": edge.key_quality,
        },
        local_error_classification="timeout"
        if timed_out
        else "cancelled"
        if cancelled
        else "none",
        payload_snippet=request_body.decode("utf-8", errors="ignore")
        if request_body is not None
        else None,
    )


def _extract_explicit_retry_observed(metadata: Mapping[str, object] | None) -> bool | None:
    if metadata is None:
        return None

    attempt = metadata.get("retry_attempt")
    if isinstance(attempt, int):
        return attempt >= 2

    is_retry = metadata.get("is_retry")
    if isinstance(is_retry, bool):
        return is_retry

    return None


def _hash_retry_identity(identity: str) -> int:
    # 32-bit FNV-1a.
    hash_value = 0x811C9DC5
    for char in identity:
        hash_value ^= ord(char) & 0xFF
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return hash_value


def _filter_headers(headers: Mapping[str, str], allowlist: list[str]) -> dict[str, str] | None:
    if not allowlist:
        return None

    out: dict[str, str] = {}
    lower = {str(k).lower(): str(v) for k, v in headers.items()}
    for name in allowlist:
        value = lower.get(name.lower())
        if value:
            out[name.lower()] = value
    return out if out else None


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None

    try:
        parsed = int(value)
    except Exception:
        return None

    return parsed if parsed >= 0 else None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized if normalized else None
    return None


def _parse_status_code(status_line: str) -> int:
    try:
        return int(status_line.split(" ", 1)[0])
    except Exception:
        return 0
