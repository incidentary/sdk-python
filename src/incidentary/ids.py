"""Identifier generators for the Incidentary Python SDK.

Exposes two functions with a deliberate split:

- :func:`new_id` — UUIDv7 (RFC 9562) for database-backed identifiers
  (trace IDs, CE IDs, anywhere sort-key locality matters).
- :func:`new_random_token` — UUIDv4 for externally visible,
  privacy-sensitive tokens (deploy dedup keys, share-URL slugs,
  anywhere the 48-bit millisecond timestamp that v7 embeds would leak
  the creation time across a trust boundary).

Binary-compatible with v4 on the wire: both share the 128-bit UUID
layout, so either form slots into a ``uuid`` column transparently.

Pure stdlib implementation. The project targets Python 3.11+, which
does not yet ship a native ``uuid.uuid7()``; the layout here matches
what a future stdlib implementation (and peer SDKs in Node/Go/.NET)
must produce. For :func:`new_random_token` we delegate to
``uuid.uuid4()`` which has always been stdlib.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["new_id", "new_random_token"]


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


def new_random_token() -> str:
    """Return a canonical UUIDv4 string.

    122 bits of CSPRNG output with no embedded timestamp. Use this for
    externally visible tokens (deploy dedup keys attached to public
    URLs, share-URL slugs, CSRF nonces) where the creation time
    embedded in a UUIDv7 would be a side channel.
    """
    return str(uuid.uuid4())
