"""Canonical UUIDv7 helper for the Incidentary Python SDK.

UUIDv7 (RFC 9562) encodes a Unix-millis timestamp in the most
significant 48 bits. Ids generated minutes apart sort lexicographically
in the order they were created, which materially improves B-tree
locality on hot ingest paths. Binary-compatible with v4 on the wire —
callers that previously emitted ``uuid.uuid4()`` can switch to
:func:`new_id` transparently.

Pure stdlib implementation. The project targets Python 3.11+, which
does not yet ship a native ``uuid.uuid7()``; the layout here matches
what a future stdlib implementation (and peer SDKs in Node/Go/.NET)
must produce.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["new_id"]


def new_id() -> str:
    """Return a canonical UUIDv7 string.

    Format (RFC 9562 §5.7):
    - 48 bits: Unix-epoch milliseconds (big-endian)
    - 4 bits: version = 7
    - 12 bits: rand_a (random)
    - 2 bits: variant = 10 (RFC 4122)
    - 62 bits: rand_b (random)
    """
    unix_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 48 bits

    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF  # 12 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF  # 62 bits

    # Pack into a 128-bit integer: [48 ts][4 ver=7][12 rand_a][2 var=10][62 rand_b]
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b

    return str(uuid.UUID(int=value))
