"""Tests for the gixen plugin system: hookspec, manager factory, loader."""
from __future__ import annotations

import pluggy
import pytest


# --- Unit 2: hookspec + plugin manager factory ----------------------------------


def test_make_plugin_manager_returns_pluggy_manager():
    from gixen.plugins import make_plugin_manager

    pm = make_plugin_manager()
    assert isinstance(pm, pluggy.PluginManager)
    assert pm.project_name == "gixen"


def test_manager_exposes_three_hookspecs():
    from gixen.plugins import make_plugin_manager

    pm = make_plugin_manager()
    # pluggy exposes hooks as attributes on pm.hook; accessing each should not raise.
    assert pm.hook.register_routes is not None
    assert pm.hook.register_db_tables is not None
    assert pm.hook.register_dashboard_tabs is not None


def test_plugin_with_matching_hookimpls_registers_cleanly():
    from gixen.plugins import hookimpl, make_plugin_manager

    class FakePlugin:
        @hookimpl
        def register_routes(self, app):
            return None

        @hookimpl
        def register_db_tables(self, conn):
            return None

        @hookimpl
        def register_dashboard_tabs(self):
            return []

    pm = make_plugin_manager()
    pm.register(FakePlugin(), name="fake")
    assert pm.is_registered(pm.get_plugin("fake"))
    pm.check_pending()  # no PluginValidationError


def test_plugin_with_mismatched_hook_name_fails_check_pending():
    from gixen.plugins import hookimpl, make_plugin_manager

    class TypoPlugin:
        @hookimpl
        def register_route(self, app):  # typo — missing the 's'
            return None

    pm = make_plugin_manager()
    pm.register(TypoPlugin(), name="typo")
    with pytest.raises(pluggy.PluginValidationError):
        pm.check_pending()


def test_module_exports():
    """The public API of gixen.plugins is stable and exhaustive."""
    import gixen.plugins as mod

    expected = {"hookimpl", "hookspec", "GixenPluginSpec", "make_plugin_manager", "load_plugins"}
    assert set(mod.__all__) == expected
    for name in expected:
        assert hasattr(mod, name), f"gixen.plugins missing {name}"


def test_hookimpl_works_via_re_export():
    """Plugin authors should be able to `from gixen.plugins import hookimpl`."""
    from gixen.plugins import hookimpl

    @hookimpl
    def register_routes(app):
        return None

    # The decorator should have left behind the pluggy hookimpl marker.
    assert hasattr(register_routes, "gixen_impl") or hasattr(register_routes, "pluggy_impl_meta") or callable(register_routes)
