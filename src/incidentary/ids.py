"""Canonical UUIDv4 id generator for the Incidentary Python SDK.

UUIDv4 (RFC 9562 §5.4) is 122 bits of CSPRNG random with no embedded
timestamp. The server accepts v1/v4/v7 transparently — the binary
representation is identical across versions — but all first-party SDKs
emit v4.

The earlier spec drafts recommended UUIDv7 on the grounds that the
48-bit millisecond prefix would improve server-side storage locality.
That reasoning was wrong for the Incidentary server schema:

- ClickHouse compares UUIDs second-half-first for historical reasons,
  so v7's timestamp prefix contributes nothing to sparse-index
  ordering or pruning.
- Every UUID-bearing ClickHouse table already carries time locality in
  an explicit ``i64`` nanosecond column that sits *before* the UUID in
  the sort key.

With the storage-locality case empty, the remaining consideration is
the v7 48-bit timestamp prefix — a recoverable creation-time side
channel for any value that might cross a trust boundary. v4 has no
such leak.

Backed by :func:`uuid.uuid4` (stdlib), which wraps ``os.urandom`` →
the OS CSPRNG. Suitable for unguessable identifiers; not a substitute
for cryptographic keys.
"""

from __future__ import annotations

import uuid

__all__ = ["new_id"]


def new_id() -> str:
    """Return a canonical UUIDv4 string (RFC 9562 §5.4)."""
    return str(uuid.uuid4())
