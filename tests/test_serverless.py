"""Tests for serverless handler wrappers."""

from unittest.mock import MagicMock

from incidentary.context import clear_trace_context, get_trace_context
from incidentary.serverless import incidentary_handler


def _make_client() -> MagicMock:
    client = MagicMock()
    client.flush_to_backend = MagicMock()
    return client


def test_handler_receives_event_and_context_returns_result():
    client = _make_client()

    @incidentary_handler(client)
    def handler(event, context):
        return {"statusCode": 200, "event": event, "ctx_keys": list(context.keys())}

    result = handler({"body": "hello"}, {"function_name": "test-fn"})

    assert result["statusCode"] == 200
    assert result["event"] == {"body": "hello"}
    assert "function_name" in result["ctx_keys"]


def test_trace_context_available_inside_handler():
    client = _make_client()
    captured = {}

    @incidentary_handler(client)
    def handler(event, context):
        ctx = get_trace_context()
        captured["trace_id"] = ctx.trace_id if ctx else None
        captured["ce_id"] = ctx.ce_id if ctx else None
        return "ok"

    handler({}, {})

    # Both IDs should be UUID-shaped strings
    import re

    uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    assert captured["trace_id"] is not None
    assert uuid_pattern.match(captured["trace_id"])
    assert captured["ce_id"] is not None
    assert uuid_pattern.match(captured["ce_id"])


def test_flush_to_backend_called_after_handler():
    client = _make_client()

    @incidentary_handler(client)
    def handler(event, context):
        return "done"

    handler({}, {})

    client.flush_to_backend.assert_called_once()


def test_flush_to_backend_called_even_when_handler_raises():
    client = _make_client()

    @incidentary_handler(client)
    def handler(event, context):
        raise ValueError("handler boom")

    try:
        handler({}, {})
    except ValueError:
        pass

    client.flush_to_backend.assert_called_once()


def test_handler_errors_are_reraised():
    client = _make_client()
    error = RuntimeError("specific error")

    @incidentary_handler(client)
    def handler(event, context):
        raise error

    raised = None
    try:
        handler({}, {})
    except RuntimeError as exc:
        raised = exc

    assert raised is error


def test_context_cleared_after_handler_completes():
    client = _make_client()
    clear_trace_context()

    @incidentary_handler(client)
    def handler(event, context):
        return "ok"

    handler({}, {})

    assert get_trace_context() is None


def test_context_cleared_after_handler_raises():
    client = _make_client()
    clear_trace_context()

    @incidentary_handler(client)
    def handler(event, context):
        raise RuntimeError("boom")

    try:
        handler({}, {})
    except RuntimeError:
        pass

    assert get_trace_context() is None


def test_flush_error_does_not_propagate():
    client = _make_client()
    client.flush_to_backend.side_effect = RuntimeError("flush exploded")

    @incidentary_handler(client)
    def handler(event, context):
        return "safe"

    # Must not raise — flush errors are swallowed
    result = handler({}, {})
    assert result == "safe"
