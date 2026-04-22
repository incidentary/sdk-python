"""Tests for the UUIDv7 / UUIDv4 identifier helpers."""

from __future__ import annotations

import re
import time
import uuid

from incidentary.ids import new_id, new_random_token


UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

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

    def test_has_v7_version_nibble(self):
        generated = new_id()
        assert UUIDV7_RE.match(generated) is not None, generated

    def test_variant_bits_match_rfc4122(self):
        generated = new_id()
        # Canonical string: group 4 starts with variant bits
        parts = generated.split("-")
        assert parts[3][0] in "89ab"

    def test_is_time_ordered_across_small_delay(self):
        first = new_id()
        time.sleep(0.002)
        second = new_id()
        # v7 encodes wall-clock ms in the leading bits, so
        # lexicographic order matches chronological order.
        assert first < second

    def test_returns_distinct_ids_within_same_millisecond(self):
        # rand_b segment provides uniqueness within a ms.
        ids = {new_id() for _ in range(256)}
        assert len(ids) == 256

    def test_timestamp_is_close_to_current_time(self):
        # The first 48 bits encode unix-millis; confirm they're within
        # a generous envelope of now (±5 s for CI jitter).
        before = int(time.time() * 1000)
        generated = new_id()
        after = int(time.time() * 1000)

        ts_hex = generated.replace("-", "")[:12]
        ts_ms = int(ts_hex, 16)
        assert before - 5_000 <= ts_ms <= after + 5_000


class TestNewRandomToken:
    def test_returns_canonical_uuid_string(self):
        generated = new_random_token()
        parsed = uuid.UUID(generated)
        assert str(parsed) == generated

    def test_has_v4_version_nibble(self):
        generated = new_random_token()
        assert UUIDV4_RE.match(generated) is not None, generated

    def test_variant_bits_match_rfc4122(self):
        parts = new_random_token().split("-")
        assert parts[3][0] in "89ab"

    def test_never_reuses_v7_version_nibble(self):
        for _ in range(64):
            parts = new_random_token().split("-")
            assert parts[2][0] == "4", f"expected v4, got {parts[2][0]}"

    def test_returns_distinct_tokens_across_many_calls(self):
        tokens = {new_random_token() for _ in range(512)}
        assert len(tokens) == 512

    def test_is_not_monotonic_by_generation_time(self):
        # Over 40 pairs we MUST see both orderings. A mis-wired
        # implementation returning v7 would always satisfy a < b.
        saw_ascending = False
        saw_descending_or_equal = False
        for _ in range(40):
            a = new_random_token()
            time.sleep(0.002)
            b = new_random_token()
            if a < b:
                saw_ascending = True
            else:
                saw_descending_or_equal = True
            if saw_ascending and saw_descending_or_equal:
                return
        raise AssertionError(
            "v4 tokens must not be monotonic by generation time"
        )
