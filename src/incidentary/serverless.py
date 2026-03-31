"""Serverless handler wrappers for AWS Lambda."""

from __future__ import annotations

import functools
import uuid
from typing import Any, Callable, TypeVar

from .context import clear_trace_context, set_trace_context

F = TypeVar("F", bound=Callable[..., Any])


def incidentary_handler(client: Any) -> Callable[[F], F]:
    """
    Decorator for Lambda handlers. Sets trace context and flushes after invocation.

    The decorator never raises from its own logic — only handler errors are re-raised.
    Flush errors are swallowed to avoid masking handler results.

    Usage:
        @incidentary_handler(client)
        def handler(event, context):
            requests.post("https://api.example.com/...")
            return {"statusCode": 200}
    """

    def decorator(handler: F) -> F:
        @functools.wraps(handler)
        def wrapper(event: Any, context: Any) -> Any:
            trace_id = str(uuid.uuid4())
            ce_id = str(uuid.uuid4())
            set_trace_context(trace_id, ce_id)
            try:
                return handler(event, context)
            finally:
                try:
                    client.flush_to_backend()
                except Exception:
                    pass
                clear_trace_context()

        return wrapper  # type: ignore[return-value]

    return decorator
