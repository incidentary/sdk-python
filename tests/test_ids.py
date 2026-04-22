"""Tests for the UUIDv4 id helper."""

from __future__ import annotations

import re
import uuid

from incidentary.ids import new_id


# Canonical UUIDv4 shape: the 14th hex character (first char of the
# third group) is '4'; the 19th hex character (first char of the
# fourth group) is one of 8/9/a/b (RFC 4122 variant bits).
UUIDV4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class TestNewId:
    def test_returns_canonical_uuid_string(self):
        generated = new_id()
        # Parseable by the stdlib — rules out invalid layouts.
        parsed = uuid.UUID(generated)
        assert str(parsed) == generated

    def test_has_v4_version_nibble(self):
        # The whole point of unifying on v4: a future refactor that
        # silently swaps uuid4() for uuid7() must not slip through
        # review. This test screams the moment the version nibble
        # stops being '4'.
        generated = new_id()
        assert UUIDV4_RE.match(generated) is not None, generated

    def test_has_version_4_via_stdlib_reflection(self):
        assert uuid.UUID(new_id()).version == 4

    def test_variant_bits_match_rfc4122(self):
        generated = new_id()
        parts = generated.split("-")
        assert parts[3][0] in "89ab"

    def test_returns_distinct_ids_across_many_samples(self):
        # v4 has 122 random bits; collision across 1024 samples is
        # effectively zero. A collision here proves the RNG is seeded
        # or deterministic — a fatal bug for bearer-token use.
        ids = {new_id() for _ in range(1024)}
        assert len(ids) == 1024

    def test_is_not_serially_ordered(self):
        # v4 has no embedded timestamp, so two ids generated
        # back-to-back must not have a systematic lexicographic
        # relationship. Guards against a regression that reinstates
        # a time-ordered generator.
        lt = gt = 0
        for _ in range(500):
            a = new_id()
            b = new_id()
            if a < b:
                lt += 1
            elif a > b:
                gt += 1
            else:
                raise AssertionError("impossible collision")
        # Each side should land in [150, 350] (well inside a 12σ
        # envelope around 250/500).
        assert 150 <= lt <= 350, f"a<b happened {lt}/500 times"
        assert 150 <= gt <= 350, f"a>b happened {gt}/500 times"
