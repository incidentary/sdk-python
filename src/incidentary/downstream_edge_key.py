"""Downstream edge key resolution for retry identity quality."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

RetryKeyQuality = Literal[
    "explicit",
    "route_template",
    "logical_edge",
    "normalized_url",
    "unknown",
]


@dataclass(frozen=True)
class DownstreamEdgeKeyResolution:
    edge_key: str
    route_key: str
    key_quality: RetryKeyQuality
    operation_key: str
    key_for_hash: str


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE
)
_NUMERIC_RE = re.compile(r"^\d+$")
_LONG_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


class DownstreamEdgeKeyResolver:
    def resolve(
        self,
        *,
        trace_id: str,
        method: str,
        url: str,
        metadata: Mapping[str, object] | None = None,
    ) -> DownstreamEdgeKeyResolution:
        md = metadata or {}
        method_norm = (method or "GET").strip().upper() or "GET"
        normalized_edge, normalized_route = _normalize_url_target(url)

        explicit_key = _first_non_empty(
            md.get("retry_group_id"),
            md.get("idempotency_key"),
            md.get("operation_key"),
            md.get("retry_key"),
        )

        if explicit_key is not None:
            edge_key = (
                _first_non_empty(md.get("edge_key"), md.get("downstream_service"), normalized_edge)
                or "unknown"
            )
            route_key = (
                _first_non_empty(md.get("route_template"), md.get("route_key"), normalized_route)
                or "/"
            )
            return DownstreamEdgeKeyResolution(
                edge_key=edge_key,
                route_key=route_key,
                key_quality="explicit",
                operation_key=explicit_key,
                key_for_hash=f"{trace_id}|{edge_key}|{method_norm}|explicit:{explicit_key}",
            )

        route_template = _first_non_empty(md.get("route_template"), md.get("route_key"))
        if route_template is not None:
            edge_key = (
                _first_non_empty(md.get("edge_key"), md.get("downstream_service"), normalized_edge)
                or "unknown"
            )
            canonical_route = _canonicalize_route(route_template)
            operation_key = f"{method_norm} {canonical_route}"
            return DownstreamEdgeKeyResolution(
                edge_key=edge_key,
                route_key=canonical_route,
                key_quality="route_template",
                operation_key=operation_key,
                key_for_hash=f"{trace_id}|{edge_key}|{operation_key}",
            )

        logical_edge = _first_non_empty(md.get("downstream_service"), md.get("edge_key"))
        operation_name = _first_non_empty(md.get("operation_name"), md.get("operation_key"))
        if logical_edge is not None or operation_name is not None:
            edge_key = logical_edge or normalized_edge
            operation_key = operation_name or f"{method_norm} {normalized_route}"
            return DownstreamEdgeKeyResolution(
                edge_key=edge_key,
                route_key=normalized_route,
                key_quality="logical_edge",
                operation_key=operation_key,
                key_for_hash=f"{trace_id}|{edge_key}|logical:{operation_key}",
            )

        if normalized_edge != "unknown" or normalized_route != "/unknown":
            operation_key = f"{method_norm} {normalized_route}"
            return DownstreamEdgeKeyResolution(
                edge_key=normalized_edge,
                route_key=normalized_route,
                key_quality="normalized_url",
                operation_key=operation_key,
                key_for_hash=f"{trace_id}|{normalized_edge}|{operation_key}",
            )

        operation_key = f"{method_norm} unknown"
        return DownstreamEdgeKeyResolution(
            edge_key="unknown",
            route_key="/unknown",
            key_quality="unknown",
            operation_key=operation_key,
            key_for_hash=f"{trace_id}|unknown|{method_norm}|unknown",
        )


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        if not isinstance(value, str):
            continue
        norm = value.strip()
        if norm:
            return norm
    return None


def _normalize_url_target(url: str) -> tuple[str, str]:
    raw = (url or "").strip()
    if not raw:
        return "unknown", "/unknown"

    scheme_index = raw.find("://")
    if scheme_index >= 0:
        host_start = scheme_index + 3
        if host_start >= len(raw):
            return "unknown", "/unknown"

        path_start = _index_of_first(raw, host_start, "/", "?", "#")
        edge_raw = raw[host_start:path_start] if path_start >= 0 else raw[host_start:]
        path_raw = raw[path_start:] if path_start >= 0 else "/"

        edge = edge_raw.strip() or "unknown"
        route = _canonicalize_route(path_raw)
        return edge, route

    return "local", _canonicalize_route(raw)


def _index_of_first(value: str, start: int, *tokens: str) -> int:
    result = -1
    for token in tokens:
        idx = value.find(token, start)
        if idx >= 0 and (result < 0 or idx < result):
            result = idx
    return result


def _canonicalize_route(route: str) -> str:
    cleaned = route or "/"
    q = cleaned.find("?")
    h = cleaned.find("#")
    end = len(cleaned)
    if q >= 0:
        end = min(end, q)
    if h >= 0:
        end = min(end, h)

    path = cleaned[:end]
    if not path.startswith("/"):
        path = "/" + path

    parts = path.split("/")
    for i, part in enumerate(parts):
        if not part:
            continue
        if _NUMERIC_RE.match(part) or _UUID_RE.match(part) or _LONG_HEX_RE.match(part):
            parts[i] = ":id"

    normalized = "/".join(parts)
    return normalized or "/"
