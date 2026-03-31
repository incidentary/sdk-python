"""Tests for psycopg2 integration (TDD — written before implementation).

psycopg2 is mocked throughout; it does not need to be installed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


def _build_fake_psycopg2():
    """Return a minimal fake psycopg2 module."""
    psycopg2_mod = MagicMock()

    class FakeCursor:
        def execute(self, query, vars=None):
            return None

        def executemany(self, query, vars_list):
            return None

    class FakeExtensions:
        cursor = FakeCursor

    psycopg2_mod.extensions = FakeExtensions
    return psycopg2_mod


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestPsycopg2Detect:
    def test_detect_returns_false_when_psycopg2_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            assert integration.detect() is False

    def test_detect_returns_true_when_psycopg2_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            integration.detect()
            mock_find_spec.assert_called_once_with("psycopg2")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestPsycopg2Name:
    def test_name_is_psycopg2(self):
        from incidentary.integrations.psycopg2_integration import Psycopg2Integration

        assert Psycopg2Integration().name == "psycopg2"


# ---------------------------------------------------------------------------
# is_patched()
# ---------------------------------------------------------------------------


class TestPsycopg2IsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.psycopg2_integration import Psycopg2Integration

        assert Psycopg2Integration().is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — cursor.execute patching
# ---------------------------------------------------------------------------


class TestPsycopg2Patch:
    def test_patch_replaces_cursor_execute(self):
        fake_psycopg2 = _build_fake_psycopg2()
        original_execute = fake_psycopg2.extensions.cursor.execute
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_psycopg2.extensions.cursor.execute is not original_execute

    def test_patch_replaces_cursor_executemany(self):
        fake_psycopg2 = _build_fake_psycopg2()
        original_executemany = fake_psycopg2.extensions.cursor.executemany
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_psycopg2.extensions.cursor.executemany is not original_executemany

    def test_patch_is_idempotent(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)
            patched_execute = fake_psycopg2.extensions.cursor.execute
            integration.patch(client)
            assert fake_psycopg2.extensions.cursor.execute is patched_execute

    def test_patch_does_not_raise_when_psycopg2_missing(self):
        from incidentary.integrations.psycopg2_integration import Psycopg2Integration

        integration = Psycopg2Integration()
        client = _make_stub_client()
        with patch.dict(sys.modules, {"psycopg2": None, "psycopg2.extensions": None}):
            integration.patch(client)  # must not raise


# ---------------------------------------------------------------------------
# cursor.execute — event recording
# ---------------------------------------------------------------------------


class TestPsycopg2ExecuteRecording:
    def test_execute_records_db_query_event(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            fake_psycopg2.extensions.cursor.execute(cursor, "SELECT 1")

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "db_query"

    def test_execute_records_event_with_internal_kind(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            fake_psycopg2.extensions.cursor.execute(cursor, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.event_attrs is not None
        assert opts.event_attrs.get("kind") == "INTERNAL"

    def test_execute_records_duration_ns(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            fake_psycopg2.extensions.cursor.execute(cursor, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.duration_ns >= 0

    def test_execute_does_not_raise_when_record_event_fails(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            fake_psycopg2.extensions.cursor.execute(cursor, "SELECT 1")  # must not raise

    def test_execute_still_raises_original_exception(self):
        fake_psycopg2 = _build_fake_psycopg2()

        class BrokenCursor(fake_psycopg2.extensions.cursor):
            def execute(self, query, vars=None):
                raise Exception("DB error")

        fake_psycopg2.extensions.cursor = BrokenCursor

        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            with pytest.raises(Exception, match="DB error"):
                fake_psycopg2.extensions.cursor.execute(cursor, "SELECT 1")

    def test_execute_records_error_status_on_exception(self):
        fake_psycopg2 = _build_fake_psycopg2()

        class BrokenCursor(fake_psycopg2.extensions.cursor):
            def execute(self, query, vars=None):
                raise Exception("DB error")

        fake_psycopg2.extensions.cursor = BrokenCursor

        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            with pytest.raises(Exception):
                fake_psycopg2.extensions.cursor.execute(cursor, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.status == 500


# ---------------------------------------------------------------------------
# Statement safety / truncation
# ---------------------------------------------------------------------------


class TestPsycopg2StatementSafety:
    def test_long_query_is_truncated_to_500_chars(self):
        from incidentary.integrations.psycopg2_integration import _safe_statement

        long_query = "SELECT " + "x" * 600
        result = _safe_statement(long_query)
        assert len(result) <= 500

    def test_short_query_is_unchanged(self):
        from incidentary.integrations.psycopg2_integration import _safe_statement

        query = "SELECT 1"
        assert _safe_statement(query) == query

    def test_non_string_query_returns_empty_string(self):
        from incidentary.integrations.psycopg2_integration import _safe_statement

        assert _safe_statement(None) == ""
        assert _safe_statement(42) == ""
        assert _safe_statement(b"SELECT 1") == ""

    def test_query_at_exactly_500_chars_is_unchanged(self):
        from incidentary.integrations.psycopg2_integration import _safe_statement

        query = "x" * 500
        assert _safe_statement(query) == query

    def test_query_at_501_chars_is_truncated(self):
        from incidentary.integrations.psycopg2_integration import _safe_statement

        query = "x" * 501
        assert len(_safe_statement(query)) == 500


# ---------------------------------------------------------------------------
# unpatch()
# ---------------------------------------------------------------------------


class TestPsycopg2Unpatch:
    def test_unpatch_restores_original_execute(self):
        fake_psycopg2 = _build_fake_psycopg2()
        original_execute = fake_psycopg2.extensions.cursor.execute
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

        assert fake_psycopg2.extensions.cursor.execute is original_execute

    def test_unpatch_restores_original_executemany(self):
        fake_psycopg2 = _build_fake_psycopg2()
        original_executemany = fake_psycopg2.extensions.cursor.executemany
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

        assert fake_psycopg2.extensions.cursor.executemany is original_executemany

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.psycopg2_integration import Psycopg2Integration

        integration = Psycopg2Integration()
        integration.unpatch()  # must not raise

    def test_unpatch_is_idempotent(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()  # must not raise


# ---------------------------------------------------------------------------
# executemany recording
# ---------------------------------------------------------------------------


class TestPsycopg2ExecutemanyRecording:
    def test_executemany_records_db_query_event(self):
        fake_psycopg2 = _build_fake_psycopg2()
        with patch.dict(
            sys.modules,
            {"psycopg2": fake_psycopg2, "psycopg2.extensions": fake_psycopg2.extensions},
        ):
            from incidentary.integrations.psycopg2_integration import Psycopg2Integration

            integration = Psycopg2Integration()
            client = _make_stub_client()
            integration.patch(client)

            cursor = fake_psycopg2.extensions.cursor()
            fake_psycopg2.extensions.cursor.executemany(
                cursor, "INSERT INTO t VALUES (%s)", [(1,), (2,)]
            )

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "db_query"


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestPsycopg2ABCConformance:
    def test_psycopg2_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.psycopg2_integration import Psycopg2Integration

        assert isinstance(Psycopg2Integration(), Integration)

    def test_psycopg2_integration_importable_from_integrations_package(self):
        from incidentary.integrations import Psycopg2Integration

        assert Psycopg2Integration is not None

    def test_default_integrations_includes_psycopg2(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.psycopg2_integration import Psycopg2Integration

        result = default_integrations()
        assert any(isinstance(i, Psycopg2Integration) for i in result)
