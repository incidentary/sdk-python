from incidentary.downstream_edge_key import DownstreamEdgeKeyResolver


def test_prefers_explicit_retry_metadata():
    resolver = DownstreamEdgeKeyResolver()
    resolved = resolver.resolve(
        trace_id="trace-1",
        method="POST",
        url="https://billing.internal/charges/123/capture?expand=true",
        metadata={
            "retry_group_id": "retry-group-77",
            "route_template": "/charges/:id/capture",
            "downstream_service": "billing",
        },
    )

    assert resolved.key_quality == "explicit"
    assert resolved.operation_key == "retry-group-77"
    assert resolved.edge_key == "billing"


def test_uses_route_templates_before_logical_or_url():
    resolver = DownstreamEdgeKeyResolver()

    a = resolver.resolve(
        trace_id="trace-2",
        method="POST",
        url="https://billing.internal/charges/123/capture",
        metadata={"route_template": "/charges/:id/capture", "downstream_service": "billing"},
    )

    b = resolver.resolve(
        trace_id="trace-2",
        method="POST",
        url="https://billing.internal/charges/456/capture",
        metadata={"route_template": "/charges/:id/capture", "downstream_service": "billing"},
    )

    assert a.key_quality == "route_template"
    assert b.key_quality == "route_template"
    assert a.key_for_hash == b.key_for_hash


def test_falls_back_to_normalized_url_and_collapses_dynamic_ids():
    resolver = DownstreamEdgeKeyResolver()

    a = resolver.resolve(
        trace_id="trace-4",
        method="GET",
        url="https://orders.internal/users/123/orders/550e8400-e29b-41d4-a716-446655440000?state=open",
    )

    b = resolver.resolve(
        trace_id="trace-4",
        method="GET",
        url="https://orders.internal/users/456/orders/550e8400-e29b-41d4-a716-446655440111?state=closed",
    )

    assert a.key_quality == "normalized_url"
    assert a.route_key == "/users/:id/orders/:id"
    assert a.key_for_hash == b.key_for_hash


def test_distinct_route_templates_do_not_merge():
    resolver = DownstreamEdgeKeyResolver()

    capture = resolver.resolve(
        trace_id="trace-5",
        method="POST",
        url="https://billing.internal/charges/123/capture",
        metadata={"route_template": "/charges/:id/capture", "downstream_service": "billing"},
    )

    refund = resolver.resolve(
        trace_id="trace-5",
        method="POST",
        url="https://billing.internal/charges/123/refund",
        metadata={"route_template": "/charges/:id/refund", "downstream_service": "billing"},
    )

    assert capture.key_for_hash != refund.key_for_hash


# ── Edge cases and negative inputs ────────────────────────────────────


_r = DownstreamEdgeKeyResolver()


def test_empty_url_returns_unknown_quality():
    resolved = _r.resolve(trace_id="t", method="GET", url="")
    assert resolved.key_quality == "unknown"
    assert resolved.edge_key == "unknown"
    assert resolved.route_key == "/unknown"


def test_whitespace_url_returns_unknown_quality():
    resolved = _r.resolve(trace_id="t", method="GET", url="   ")
    assert resolved.key_quality == "unknown"
    assert resolved.edge_key == "unknown"


def test_empty_method_defaults_to_get():
    resolved = _r.resolve(trace_id="t", method="", url="https://svc.internal/api")
    assert resolved.operation_key.startswith("GET ")
    assert resolved.key_quality == "normalized_url"


def test_none_metadata_does_not_crash():
    resolved = _r.resolve(trace_id="t", method="GET", url="https://svc.internal/api", metadata=None)
    assert resolved.key_quality == "normalized_url"
    assert resolved.edge_key == "svc.internal"


def test_scheme_only_url():
    resolved = _r.resolve(trace_id="t", method="GET", url="https://")
    assert resolved.key_quality == "unknown"
    assert resolved.edge_key == "unknown"


def test_url_with_no_path_component():
    resolved = _r.resolve(trace_id="t", method="GET", url="https://service.internal")
    assert resolved.key_quality == "normalized_url"
    assert resolved.edge_key == "service.internal"
    assert resolved.route_key == "/"


def test_metadata_with_all_whitespace_fields():
    resolved = _r.resolve(
        trace_id="t",
        method="GET",
        url="https://svc.internal/api",
        metadata={"retry_group_id": "  ", "route_template": "  "},
    )
    assert resolved.key_quality == "normalized_url"
    assert resolved.edge_key == "svc.internal"


def test_metadata_with_non_string_values():
    resolved = _r.resolve(
        trace_id="t",
        method="GET",
        url="https://svc.internal/api",
        metadata={"retry_group_id": 42, "downstream_service": True},
    )
    assert resolved.key_quality == "normalized_url"
    assert resolved.edge_key == "svc.internal"


def test_path_only_url():
    resolved = _r.resolve(trace_id="t", method="GET", url="/api/v1/users/123")
    assert resolved.key_quality == "normalized_url"
    assert resolved.edge_key == "local"
    assert resolved.route_key == "/api/v1/users/:id"


def test_url_with_query_only_no_path():
    resolved = _r.resolve(trace_id="t", method="GET", url="https://svc?foo=bar")
    assert resolved.edge_key == "svc"
    assert resolved.route_key == "/"


def test_very_long_url():
    resolved = _r.resolve(trace_id="t", method="GET", url="https://svc.internal/" + "a" * 10_000)
    assert resolved.key_quality == "normalized_url"
    assert resolved.edge_key == "svc.internal"
