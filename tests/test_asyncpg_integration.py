"""Tests for asyncpg integration (TDD — written before implementation).

asyncpg is mocked throughout; it does not need to be installed.
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


def _build_fake_asyncpg():
    """Return a minimal fake asyncpg module."""
    asyncpg_mod = MagicMock()

    class FakeConnection:
        async def execute(self, query, *args, **kwargs):
            return "RESULT"

        async def fetch(self, query, *args, **kwargs):
            return []

        async def fetchval(self, query, *args, **kwargs):
            return None

        async def fetchrow(self, query, *args, **kwargs):
            return None

    asyncpg_mod.Connection = FakeConnection
    return asyncpg_mod


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestAsyncpgDetect:
    def test_detect_returns_false_when_asyncpg_not_installed(self):
        import importlib.util

        with patch.object(importlib.util, "find_spec", return_value=None):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            assert integration.detect() is False

    def test_detect_returns_true_when_asyncpg_available(self):
        spec_mock = MagicMock()
        with patch("importlib.util.find_spec", return_value=spec_mock):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            assert integration.detect() is True

    def test_detect_uses_find_spec_not_import(self):
        with patch("importlib.util.find_spec") as mock_find_spec:
            mock_find_spec.return_value = None
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            integration.detect()
            mock_find_spec.assert_called_once_with("asyncpg")


# ---------------------------------------------------------------------------
# name
# ---------------------------------------------------------------------------


class TestAsyncpgName:
    def test_name_is_asyncpg(self):
        from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

        assert AsyncpgIntegration().name == "asyncpg"


# ---------------------------------------------------------------------------
# is_patched()
# ---------------------------------------------------------------------------


class TestAsyncpgIsPatched:
    def test_is_patched_false_initially(self):
        from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

        assert AsyncpgIntegration().is_patched() is False

    def test_is_patched_true_after_patch(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)
            assert integration.is_patched() is True

    def test_is_patched_false_after_unpatch(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            assert integration.is_patched() is False


# ---------------------------------------------------------------------------
# patch() — Connection method patching
# ---------------------------------------------------------------------------


class TestAsyncpgPatch:
    def test_patch_replaces_execute(self):
        fake_asyncpg = _build_fake_asyncpg()
        original = fake_asyncpg.Connection.execute
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_asyncpg.Connection.execute is not original

    def test_patch_replaces_fetch(self):
        fake_asyncpg = _build_fake_asyncpg()
        original = fake_asyncpg.Connection.fetch
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_asyncpg.Connection.fetch is not original

    def test_patch_replaces_fetchval(self):
        fake_asyncpg = _build_fake_asyncpg()
        original = fake_asyncpg.Connection.fetchval
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_asyncpg.Connection.fetchval is not original

    def test_patch_replaces_fetchrow(self):
        fake_asyncpg = _build_fake_asyncpg()
        original = fake_asyncpg.Connection.fetchrow
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

        assert fake_asyncpg.Connection.fetchrow is not original

    def test_patch_is_idempotent(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)
            patched_execute = fake_asyncpg.Connection.execute
            integration.patch(client)
            assert fake_asyncpg.Connection.execute is patched_execute

    def test_patch_does_not_raise_when_asyncpg_missing(self):
        from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

        integration = AsyncpgIntegration()
        client = _make_stub_client()
        with patch.dict(sys.modules, {"asyncpg": None}):
            integration.patch(client)  # must not raise


# ---------------------------------------------------------------------------
# async Connection.execute — event recording
# ---------------------------------------------------------------------------


class TestAsyncpgExecuteRecording:
    async def test_execute_records_db_query_event(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.execute(conn, "SELECT 1")

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "db_query"

    async def test_execute_records_event_with_internal_kind(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.execute(conn, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.event_attrs is not None
        assert opts.event_attrs.get("kind") == "INTERNAL"

    async def test_execute_records_duration_ns(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.execute(conn, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.duration_ns >= 0

    async def test_execute_does_not_raise_when_record_event_fails(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            client.record_event.side_effect = RuntimeError("boom")
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.execute(conn, "SELECT 1")  # must not raise

    async def test_execute_still_raises_original_exception(self):
        fake_asyncpg = _build_fake_asyncpg()

        class BrokenConnection(fake_asyncpg.Connection):
            async def execute(self, query, *args, **kwargs):
                raise Exception("DB error")

        fake_asyncpg.Connection = BrokenConnection

        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            with pytest.raises(Exception, match="DB error"):
                await fake_asyncpg.Connection.execute(conn, "SELECT 1")

    async def test_execute_records_error_status_on_exception(self):
        fake_asyncpg = _build_fake_asyncpg()

        class BrokenConnection(fake_asyncpg.Connection):
            async def execute(self, query, *args, **kwargs):
                raise Exception("DB error")

        fake_asyncpg.Connection = BrokenConnection

        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            with pytest.raises(Exception):
                await fake_asyncpg.Connection.execute(conn, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.status == 500

    async def test_execute_returns_original_result(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            result = await fake_asyncpg.Connection.execute(conn, "SELECT 1")

        assert result == "RESULT"


# ---------------------------------------------------------------------------
# async Connection.fetch / fetchval / fetchrow — basic recording
# ---------------------------------------------------------------------------


class TestAsyncpgFetchRecording:
    async def test_fetch_records_db_query_event(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.fetch(conn, "SELECT * FROM t")

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "db_query"

    async def test_fetchval_records_db_query_event(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.fetchval(conn, "SELECT COUNT(*) FROM t")

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "db_query"

    async def test_fetchrow_records_db_query_event(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            await fake_asyncpg.Connection.fetchrow(conn, "SELECT * FROM t WHERE id=$1", 1)

        client.record_event.assert_called()
        event_type = client.record_event.call_args[0][0]
        assert event_type == "db_query"


# ---------------------------------------------------------------------------
# unpatch()
# ---------------------------------------------------------------------------


class TestAsyncpgUnpatch:
    def test_unpatch_restores_original_execute(self):
        fake_asyncpg = _build_fake_asyncpg()
        original = fake_asyncpg.Connection.execute
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

        assert fake_asyncpg.Connection.execute is original

    def test_unpatch_restores_all_methods(self):
        fake_asyncpg = _build_fake_asyncpg()
        originals = {
            "execute": fake_asyncpg.Connection.execute,
            "fetch": fake_asyncpg.Connection.fetch,
            "fetchval": fake_asyncpg.Connection.fetchval,
            "fetchrow": fake_asyncpg.Connection.fetchrow,
        }
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()

        for method_name, original in originals.items():
            assert getattr(fake_asyncpg.Connection, method_name) is original

    def test_unpatch_without_patch_does_not_raise(self):
        from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

        integration = AsyncpgIntegration()
        integration.unpatch()  # must not raise

    def test_unpatch_is_idempotent(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)
            integration.unpatch()
            integration.unpatch()  # must not raise


# ---------------------------------------------------------------------------
# Trace context propagation
# ---------------------------------------------------------------------------


class TestAsyncpgTraceContext:
    async def test_execute_uses_active_trace_context(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.context import clear_trace_context, set_trace_context
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            set_trace_context("trace-asyncpg-1", "ce-asyncpg-1")
            try:
                await fake_asyncpg.Connection.execute(conn, "SELECT 1")
            finally:
                clear_trace_context()

        opts = client.record_event.call_args[0][1]
        assert opts.trace_id == "trace-asyncpg-1"
        assert opts.parent_ce_id == "ce-asyncpg-1"

    async def test_execute_uses_none_trace_when_no_context(self):
        fake_asyncpg = _build_fake_asyncpg()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            from incidentary.context import clear_trace_context
            from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

            integration = AsyncpgIntegration()
            client = _make_stub_client()
            integration.patch(client)

            conn = fake_asyncpg.Connection()
            clear_trace_context()
            await fake_asyncpg.Connection.execute(conn, "SELECT 1")

        opts = client.record_event.call_args[0][1]
        assert opts.trace_id is None
        assert opts.parent_ce_id is None


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


class TestAsyncpgABCConformance:
    def test_asyncpg_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.asyncpg_integration import AsyncpgIntegration
        from incidentary.integrations.base import Integration

        assert isinstance(AsyncpgIntegration(), Integration)

    def test_asyncpg_integration_importable_from_integrations_package(self):
        from incidentary.integrations import AsyncpgIntegration

        assert AsyncpgIntegration is not None

    def test_default_integrations_includes_asyncpg(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.asyncpg_integration import AsyncpgIntegration

        result = default_integrations()
        assert any(isinstance(i, AsyncpgIntegration) for i in result)
