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
