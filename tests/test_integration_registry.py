"""Tests for the integration registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from incidentary.auto_instrument import is_patched, undo_patches

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_client() -> MagicMock:
    client = MagicMock()
    client.service_name = "test-svc"
    return client


def _make_integration(
    name: str = "test",
    detect_returns: bool = True,
    patched: bool = False,
) -> MagicMock:
    """Build a mock Integration with sensible defaults."""
    integration = MagicMock()
    integration.name = name
    integration.detect.return_value = detect_returns
    integration.is_patched.return_value = patched
    return integration


# ---------------------------------------------------------------------------
# Integration ABC contract
# ---------------------------------------------------------------------------


class TestIntegrationABC:
    """Integration subclasses must implement all abstract methods."""

    def test_integration_abc_is_importable(self):
        from incidentary.integrations.base import Integration

        assert Integration is not None

    def test_integration_abc_cannot_be_instantiated(self):
        from incidentary.integrations.base import Integration

        with pytest.raises(TypeError):
            Integration()  # type: ignore[abstract]

    def test_integration_abc_requires_name_property(self):
        from incidentary.integrations.base import Integration

        # A concrete subclass missing `name` should still fail to instantiate.
        class Incomplete(Integration):
            def detect(self) -> bool:
                return True

            def patch(self, client) -> None:  # type: ignore[override]
                pass

            def unpatch(self) -> None:
                pass

            def is_patched(self) -> bool:
                return False

        with pytest.raises(TypeError):
            Incomplete()

    def test_integration_abc_full_implementation_succeeds(self):
        from incidentary.integrations.base import Integration

        class Complete(Integration):
            @property
            def name(self) -> str:
                return "complete"

            def detect(self) -> bool:
                return True

            def patch(self, client) -> None:  # type: ignore[override]
                pass

            def unpatch(self) -> None:
                pass

            def is_patched(self) -> bool:
                return False

        instance = Complete()
        assert instance.name == "complete"
        assert instance.detect() is True
        assert instance.is_patched() is False


# ---------------------------------------------------------------------------
# IntegrationRegistry — registration
# ---------------------------------------------------------------------------


class TestRegistryRegistration:
    def test_empty_registry_has_no_registered_integrations(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        assert reg.registered == []

    def test_register_adds_integration(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        integration = _make_integration("http")
        reg.register(integration)
        assert integration in reg.registered

    def test_register_multiple_integrations(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        a = _make_integration("a")
        b = _make_integration("b")
        reg.register(a)
        reg.register(b)
        assert len(reg.registered) == 2
        assert a in reg.registered
        assert b in reg.registered

    def test_registered_returns_a_copy(self):
        """Mutating the returned list must not affect the registry."""
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        a = _make_integration("a")
        reg.register(a)
        listed = reg.registered
        listed.clear()
        assert len(reg.registered) == 1


# ---------------------------------------------------------------------------
# IntegrationRegistry — discover_and_patch
# ---------------------------------------------------------------------------


class TestDiscoverAndPatch:
    def test_detected_integration_is_patched(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        integration = _make_integration("http", detect_returns=True)
        reg.register(integration)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        integration.detect.assert_called_once()
        integration.patch.assert_called_once_with(client)

    def test_undetected_integration_is_not_patched(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        integration = _make_integration("missing", detect_returns=False)
        reg.register(integration)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        integration.detect.assert_called_once()
        integration.patch.assert_not_called()

    def test_detect_raising_skips_integration(self):
        """An exception from detect() must not crash the registry."""
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        bad = _make_integration("bad")
        bad.detect.side_effect = RuntimeError("boom")
        reg.register(bad)

        client = _make_stub_client()
        # Must not raise
        reg.discover_and_patch(client)

        bad.patch.assert_not_called()

    def test_patch_raising_does_not_crash_registry(self):
        """An exception from patch() must not crash the registry."""
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        bad = _make_integration("bad", detect_returns=True)
        bad.patch.side_effect = RuntimeError("patch exploded")
        reg.register(bad)

        client = _make_stub_client()
        # Must not raise
        reg.discover_and_patch(client)

    def test_patch_failure_does_not_prevent_subsequent_integrations(self):
        """If integration A's patch() raises, integration B must still be patched."""
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        bad = _make_integration("bad", detect_returns=True)
        bad.patch.side_effect = RuntimeError("explode")
        good = _make_integration("good", detect_returns=True)

        reg.register(bad)
        reg.register(good)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        good.patch.assert_called_once_with(client)

    def test_active_list_updated_after_patching(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        integration = _make_integration("http", detect_returns=True)
        integration.is_patched.return_value = True  # after patch() succeeds
        reg.register(integration)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        # The integration should appear in active after patching
        assert integration in reg.active

    def test_undetected_integration_not_in_active(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        integration = _make_integration("ghost", detect_returns=False)
        reg.register(integration)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        assert integration not in reg.active


# ---------------------------------------------------------------------------
# IntegrationRegistry — active property
# ---------------------------------------------------------------------------


class TestActiveProperty:
    def test_active_is_empty_before_patching(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        reg.register(_make_integration("a"))
        assert reg.active == []

    def test_active_returns_only_patched_integrations(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        patched = _make_integration("patched", detect_returns=True)
        patched.is_patched.return_value = True
        unpatched = _make_integration("unpatched", detect_returns=False)
        unpatched.is_patched.return_value = False

        reg.register(patched)
        reg.register(unpatched)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        active = reg.active
        assert patched in active
        assert unpatched not in active

    def test_active_returns_a_copy(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        a = _make_integration("a", detect_returns=True)
        a.is_patched.return_value = True
        reg.register(a)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        snapshot = reg.active
        snapshot.clear()
        assert len(reg.active) == 1


# ---------------------------------------------------------------------------
# IntegrationRegistry — unpatch_all
# ---------------------------------------------------------------------------


class TestUnpatchAll:
    def test_unpatch_all_calls_unpatch_on_active_integrations(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        a = _make_integration("a", detect_returns=True)
        a.is_patched.return_value = True
        b = _make_integration("b", detect_returns=True)
        b.is_patched.return_value = True

        reg.register(a)
        reg.register(b)

        client = _make_stub_client()
        reg.discover_and_patch(client)
        reg.unpatch_all()

        a.unpatch.assert_called_once()
        b.unpatch.assert_called_once()

    def test_unpatch_all_does_not_call_unpatch_on_inactive_integrations(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        inactive = _make_integration("ghost", detect_returns=False)
        inactive.is_patched.return_value = False
        reg.register(inactive)

        client = _make_stub_client()
        reg.discover_and_patch(client)
        reg.unpatch_all()

        inactive.unpatch.assert_not_called()

    def test_unpatch_raising_does_not_crash_registry(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        bad = _make_integration("bad", detect_returns=True)
        bad.is_patched.return_value = True
        bad.unpatch.side_effect = RuntimeError("unpatch exploded")
        reg.register(bad)

        client = _make_stub_client()
        reg.discover_and_patch(client)

        # Must not raise
        reg.unpatch_all()

    def test_unpatch_failure_does_not_prevent_subsequent_unpatches(self):
        from incidentary.integrations.registry import IntegrationRegistry

        reg = IntegrationRegistry()
        bad = _make_integration("bad", detect_returns=True)
        bad.is_patched.return_value = True
        bad.unpatch.side_effect = RuntimeError("explode")
        good = _make_integration("good", detect_returns=True)
        good.is_patched.return_value = True

        reg.register(bad)
        reg.register(good)

        client = _make_stub_client()
        reg.discover_and_patch(client)
        reg.unpatch_all()

        good.unpatch.assert_called_once()


# ---------------------------------------------------------------------------
# HTTPIntegration
# ---------------------------------------------------------------------------


class TestHTTPIntegration:
    def setup_method(self):
        undo_patches()

    def teardown_method(self):
        undo_patches()

    def test_http_integration_importable(self):
        from incidentary.integrations.http import HTTPIntegration

        assert HTTPIntegration is not None

    def test_http_integration_name(self):
        from incidentary.integrations.http import HTTPIntegration

        assert HTTPIntegration().name == "http"

    def test_detect_always_returns_true(self):
        from incidentary.integrations.http import HTTPIntegration

        assert HTTPIntegration().detect() is True

    def test_patch_sets_patched_state(self):
        from incidentary.integrations.http import HTTPIntegration

        http = HTTPIntegration()
        assert http.is_patched() is False

        client = _make_stub_client()
        http.patch(client)

        assert http.is_patched() is True

    def test_unpatch_clears_patched_state(self):
        from incidentary.integrations.http import HTTPIntegration

        http = HTTPIntegration()
        client = _make_stub_client()
        http.patch(client)
        assert http.is_patched() is True

        http.unpatch()
        assert http.is_patched() is False

    def test_patch_delegates_to_auto_instrument(self):
        from incidentary.integrations.http import HTTPIntegration

        http = HTTPIntegration()
        client = _make_stub_client()
        http.patch(client)

        # The underlying auto_instrument module must now report patched
        assert is_patched() is True

    def test_unpatch_delegates_to_undo_patches(self):
        from incidentary.integrations.http import HTTPIntegration

        http = HTTPIntegration()
        client = _make_stub_client()
        http.patch(client)
        http.unpatch()

        assert is_patched() is False

    def test_http_integration_is_instance_of_integration_abc(self):
        from incidentary.integrations.base import Integration
        from incidentary.integrations.http import HTTPIntegration

        assert isinstance(HTTPIntegration(), Integration)


# ---------------------------------------------------------------------------
# default_integrations() factory
# ---------------------------------------------------------------------------


class TestDefaultIntegrations:
    def setup_method(self):
        undo_patches()

    def teardown_method(self):
        undo_patches()

    def test_default_integrations_importable(self):
        from incidentary.integrations import default_integrations

        assert default_integrations is not None

    def test_default_integrations_returns_list(self):
        from incidentary.integrations import default_integrations

        result = default_integrations()
        assert isinstance(result, list)

    def test_default_integrations_includes_http(self):
        from incidentary.integrations import default_integrations
        from incidentary.integrations.http import HTTPIntegration

        result = default_integrations()
        assert any(isinstance(i, HTTPIntegration) for i in result)

    def test_default_integrations_returns_new_instances_each_call(self):
        """Each call must return fresh instances, not the same objects."""
        from incidentary.integrations import default_integrations

        first = default_integrations()
        second = default_integrations()
        for a, b in zip(first, second, strict=False):
            assert a is not b


# ---------------------------------------------------------------------------
# Client integration
# ---------------------------------------------------------------------------


class TestClientIntegration:
    def setup_method(self):
        undo_patches()

    def teardown_method(self):
        undo_patches()

    def _make_client(self, **overrides) -> object:
        from incidentary.client import IncidentaryClient

        config = {
            "api_key": "test",
            "service_name": "svc",
            "base_url": "http://localhost:18080",
            "pre_arm_enable_slow_success": False,
            "pre_arm_enable_inflight": False,
            "pre_arm_enable_retry": False,
        }
        config.update(overrides)
        return IncidentaryClient(**config)

    def test_client_has_registry_when_auto_instrument_true(self):
        from incidentary.integrations.registry import IntegrationRegistry

        client = self._make_client(auto_instrument=True)
        assert hasattr(client, "_registry")
        assert isinstance(client._registry, IntegrationRegistry)

    def test_client_has_no_registry_when_auto_instrument_false(self):
        client = self._make_client(auto_instrument=False)
        # registry may be None or absent, but must not raise
        registry = getattr(client, "_registry", None)
        assert registry is None

    def test_auto_instrument_true_activates_http_patching(self):
        self._make_client(auto_instrument=True)
        assert is_patched() is True

    def test_auto_instrument_false_leaves_http_unpatched(self):
        self._make_client(auto_instrument=False)
        assert is_patched() is False

    def test_custom_integrations_replaces_defaults(self):
        """Providing integrations= uses only those, not defaults."""
        from incidentary.integrations.base import Integration

        class NoopIntegration(Integration):
            @property
            def name(self) -> str:
                return "noop"

            def detect(self) -> bool:
                return True

            def patch(self, client) -> None:  # type: ignore[override]
                pass

            def unpatch(self) -> None:
                pass

            def is_patched(self) -> bool:
                return False

        noop = NoopIntegration()
        client = self._make_client(auto_instrument=True, integrations=[noop])

        # The default HTTP integration should NOT have been applied
        assert is_patched() is False
        # Our custom integration was tried
        assert client._registry is not None

    def test_empty_integrations_list_patches_nothing(self):
        self._make_client(auto_instrument=True, integrations=[])
        assert is_patched() is False

    def test_registry_stored_on_client_contains_registered_integrations(self):

        client = self._make_client(auto_instrument=True)
        registered_names = [i.name for i in client._registry.registered]
        assert "http" in registered_names


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Existing auto_instrument API must remain unchanged."""

    def setup_method(self):
        undo_patches()

    def teardown_method(self):
        undo_patches()

    def test_auto_instrument_function_still_exported_from_module(self):
        from incidentary.auto_instrument import auto_instrument as ai

        assert callable(ai)

    def test_undo_patches_still_exported_from_module(self):
        from incidentary.auto_instrument import undo_patches as up

        assert callable(up)

    def test_is_patched_still_exported_from_module(self):
        from incidentary.auto_instrument import is_patched as ip

        assert callable(ip)

    def test_auto_instrument_function_still_works(self):
        from incidentary.auto_instrument import auto_instrument as ai

        client = _make_stub_client()
        ai(client)
        assert is_patched() is True

    def test_undo_patches_still_works(self):
        from incidentary.auto_instrument import auto_instrument as ai
        from incidentary.auto_instrument import undo_patches as up

        client = _make_stub_client()
        ai(client)
        up()
        assert is_patched() is False

    def test_package_level_exports_include_new_symbols(self):
        """Integration, IntegrationRegistry, HTTPIntegration, default_integrations must be
        importable from the top-level incidentary package."""
        import incidentary

        assert hasattr(incidentary, "Integration")
        assert hasattr(incidentary, "IntegrationRegistry")
        assert hasattr(incidentary, "HTTPIntegration")
        assert hasattr(incidentary, "default_integrations")

    def test_package_level_old_exports_still_present(self):
        import incidentary

        assert hasattr(incidentary, "auto_instrument")
        assert hasattr(incidentary, "undo_patches")
        assert hasattr(incidentary, "is_patched")
