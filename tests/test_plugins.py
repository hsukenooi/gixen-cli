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


def test_module_exports_only_what_plugin_authors_need():
    """Public __all__ is just hookimpl and load_plugins; host-side primitives
    are accessible by attribute but intentionally not exported."""
    import gixen.plugins as mod

    assert set(mod.__all__) == {"hookimpl", "load_plugins"}
    # Host-side primitives must still be importable by name.
    for name in ("hookspec", "GixenPluginSpec", "make_plugin_manager"):
        assert hasattr(mod, name), f"gixen.plugins missing {name}"


def test_hookimpl_applies_pluggy_marker():
    """`from gixen.plugins import hookimpl` produces a decorator that marks
    the function with pluggy's project-scoped impl attribute."""
    from gixen.plugins import hookimpl

    @hookimpl
    def register_routes(app):
        return None

    # pluggy stores the impl marker under `<project_name>_impl` — `gixen_impl`
    # for our manager. This is the load-bearing assertion: if the re-export
    # ever broke, this attribute would be missing.
    assert hasattr(register_routes, "gixen_impl")


# --- Unit 3: loader + entry-point discovery -------------------------------------


def _plugin_module(name: str, **hooks):
    """Build a synthetic module representing a plugin."""
    import types

    from gixen.plugins import hookimpl

    mod = types.ModuleType(name)
    for hook_name, fn in hooks.items():
        setattr(mod, hook_name, hookimpl(fn))
    return mod


def test_load_plugins_with_no_plugins_installed_returns_empty_manager(fake_entry_points):
    from gixen.plugins import load_plugins

    fake_entry_points({})
    pm = load_plugins()
    assert pm.list_name_plugin() == []


def test_load_plugins_single_plugin(fake_entry_points):
    from gixen.plugins import load_plugins

    plugin = _plugin_module("solo", register_routes=lambda app: None)
    fake_entry_points({"solo": plugin})

    pm = load_plugins()
    names = [n for n, _ in pm.list_name_plugin()]
    assert names == ["solo"]


def test_load_plugins_sorts_by_entry_point_name(fake_entry_points):
    """Three plugins named b/a/c — registration order is a/b/c."""
    from gixen.plugins import load_plugins

    fake_entry_points({
        "b": _plugin_module("b", register_routes=lambda app: None),
        "a": _plugin_module("a", register_routes=lambda app: None),
        "c": _plugin_module("c", register_routes=lambda app: None),
    })
    pm = load_plugins()
    names = [n for n, _ in pm.list_name_plugin()]
    assert names == ["a", "b", "c"]


def test_load_plugins_isolates_import_failure(caplog, monkeypatch):
    """A plugin whose ep.load() raises is logged and skipped; others register."""
    import sys
    from importlib.metadata import EntryPoint

    from gixen.plugins import load_plugins

    # Build one healthy plugin and one entry point that points at a broken module.
    healthy = _plugin_module("healthy", register_routes=lambda app: None)
    monkeypatch.setitem(sys.modules, "_test_healthy_module", healthy)

    # Broken entry point: target module raises on import. We simulate this by
    # pointing at a name that ep.load() can't resolve.
    broken_ep = EntryPoint(
        name="broken", value="this_module_does_not_exist_xyz", group="gixen.plugins"
    )
    healthy_ep = EntryPoint(
        name="healthy", value="_test_healthy_module", group="gixen.plugins"
    )

    def fake_entry_points_fn(group: str):
        return [broken_ep, healthy_ep] if group == "gixen.plugins" else []

    monkeypatch.setattr("gixen.plugins.entry_points", fake_entry_points_fn)

    import logging
    caplog.set_level(logging.ERROR, logger="gixen.plugins")
    pm = load_plugins()

    names = [n for n, _ in pm.list_name_plugin()]
    assert names == ["healthy"]
    assert any("broken" in r.message for r in caplog.records)


def test_load_plugins_isolates_registration_failure(caplog, monkeypatch):
    """A plugin whose pm.register() raises (e.g. duplicate name) is logged + skipped."""
    import logging
    import sys
    from importlib.metadata import EntryPoint

    from gixen.plugins import load_plugins

    # Two distinct plugins, both claiming entry-point name "dup". pluggy
    # enforces name uniqueness on the second register() call. Build the
    # entry-points list directly (a dict literal would collapse the duplicate
    # key into one entry).
    plug1 = _plugin_module("dup1", register_routes=lambda app: None)
    plug2 = _plugin_module("dup2", register_routes=lambda app: None)
    monkeypatch.setitem(sys.modules, "_test_dup_a", plug1)
    monkeypatch.setitem(sys.modules, "_test_dup_b", plug2)
    eps = [
        EntryPoint(name="dup", value="_test_dup_a", group="gixen.plugins"),
        EntryPoint(name="dup", value="_test_dup_b", group="gixen.plugins"),
    ]
    monkeypatch.setattr("gixen.plugins.entry_points", lambda group: eps if group == "gixen.plugins" else [])

    caplog.set_level(logging.ERROR, logger="gixen.plugins")
    pm = load_plugins()

    # First "dup" registers; second is rejected by pluggy and logged.
    names = [n for n, _ in pm.list_name_plugin()]
    assert names == ["dup"]
    assert any("dup" in r.message and "register" in r.message.lower() for r in caplog.records)


def test_load_plugins_accepts_plugin_with_no_hookimpls(fake_entry_points):
    """A plugin module with no @hookimpl functions registers silently."""
    import types

    from gixen.plugins import load_plugins

    empty = types.ModuleType("empty")
    fake_entry_points({"empty": empty})
    pm = load_plugins()
    assert [n for n, _ in pm.list_name_plugin()] == ["empty"]


def test_load_plugins_supports_hyphenated_names(fake_entry_points):
    from gixen.plugins import load_plugins

    plug = _plugin_module("gixen_overlay", register_routes=lambda app: None)
    fake_entry_points({"gixen-overlay": plug})
    pm = load_plugins()
    assert [n for n, _ in pm.list_name_plugin()] == ["gixen-overlay"]


def test_load_plugins_logs_misspelled_hookspec(fake_entry_points, caplog):
    """A plugin with a misspelled hook name triggers check_pending and is logged."""
    import logging
    import types

    from gixen.plugins import hookimpl, load_plugins

    typo = types.ModuleType("typo_module")
    typo.register_route = hookimpl(lambda app: None)  # missing 's'
    fake_entry_points({"typo": typo})

    caplog.set_level(logging.ERROR, logger="gixen.plugins")
    pm = load_plugins()

    # check_pending raised but the loader logged + continued.
    assert any("validation" in r.message.lower() or "register_route" in r.message for r in caplog.records)


def test_load_plugins_end_to_end_hook_invocation(fake_entry_points):
    """Discovery -> registration -> bulk hook invocation actually fires the impl."""
    from gixen.plugins import load_plugins

    state = {"touched": False}

    def register_routes(app):
        state["touched"] = True

    plug = _plugin_module("e2e", register_routes=register_routes)
    fake_entry_points({"e2e": plug})

    pm = load_plugins()
    pm.hook.register_routes(app=object())
    assert state["touched"] is True


# --- PER-26 Unit 3: M-01 host-side helpers --------------------------------------


def _conn():
    """Fresh in-memory SQLite connection — caller closes."""
    import sqlite3
    return sqlite3.connect(":memory:")


def test_invoke_db_tables_isolated_with_no_plugins_returns_empty(fake_entry_points):
    """No plugins → empty list, no DDL run."""
    import logging
    from gixen.plugins import _invoke_db_tables_isolated, load_plugins

    fake_entry_points({})
    pm = load_plugins()
    conn = _conn()
    try:
        result = _invoke_db_tables_isolated(pm, conn, logger=logging.getLogger("test"))
        assert result == []
    finally:
        conn.close()


def test_invoke_db_tables_isolated_succeeds_with_one_plugin(fake_entry_points):
    from gixen.plugins import _invoke_db_tables_isolated, load_plugins

    def register_db_tables(conn):
        conn.execute("CREATE TABLE good_t (id INTEGER PRIMARY KEY)")

    plug = _plugin_module("good_plug", register_db_tables=register_db_tables)
    fake_entry_points({"good": plug})

    import logging
    pm = load_plugins()
    conn = _conn()
    try:
        result = _invoke_db_tables_isolated(pm, conn, logger=logging.getLogger("test"))
        assert result == ["good"]
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='good_t'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_invoke_db_tables_isolated_rolls_back_on_failure(fake_entry_points, caplog):
    """A plugin whose DDL raises mid-statement has its savepoint rolled back."""
    import logging
    from gixen.plugins import _invoke_db_tables_isolated, load_plugins

    def register_db_tables(conn):
        conn.execute("CREATE TABLE doomed (id INTEGER PRIMARY KEY)")
        raise RuntimeError("nope")

    plug = _plugin_module("bad_plug", register_db_tables=register_db_tables)
    fake_entry_points({"bad": plug})

    pm = load_plugins()
    conn = _conn()
    caplog.set_level(logging.ERROR, logger="invoke-test")
    try:
        result = _invoke_db_tables_isolated(
            pm, conn, logger=logging.getLogger("invoke-test")
        )
        assert result == []
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='doomed'"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_invoke_db_tables_isolated_handles_executescript_violation(
    fake_entry_points, caplog,
):
    """A plugin that calls conn.executescript() destroys the savepoint via an
    implicit COMMIT. The inner ROLLBACK TO raises OperationalError; the helper
    must catch the secondary failure and continue. (Regression for PER-25
    ADV-001 / REL-01 / COR-01.)"""
    import logging
    from gixen.plugins import _invoke_db_tables_isolated, load_plugins

    def register_db_tables(conn):
        conn.executescript("CREATE TABLE es_bad (id INTEGER);")
        raise RuntimeError("plugin raised after executescript")

    plug = _plugin_module("es_plug", register_db_tables=register_db_tables)
    fake_entry_points({"es-plug": plug})

    pm = load_plugins()
    conn = _conn()
    caplog.set_level(logging.ERROR, logger="invoke-test")
    try:
        result = _invoke_db_tables_isolated(
            pm, conn, logger=logging.getLogger("invoke-test")
        )
        assert result == []
    finally:
        conn.close()
    assert any(
        "Savepoint cleanup failed" in r.message
        for r in caplog.records
    )


def test_invoke_db_tables_isolated_continues_after_one_plugin_fails(
    fake_entry_points,
):
    """First plugin's DDL fails; second plugin still runs."""
    import logging
    from gixen.plugins import _invoke_db_tables_isolated, load_plugins

    def fail(conn):
        conn.execute("CREATE TABLE first_doomed (id INTEGER)")
        raise RuntimeError("nope")

    def ok(conn):
        conn.execute("CREATE TABLE second_ok (id INTEGER)")

    plug_a = _plugin_module("a_fail", register_db_tables=fail)
    plug_b = _plugin_module("b_ok", register_db_tables=ok)
    fake_entry_points({"a": plug_a, "b": plug_b})

    pm = load_plugins()
    conn = _conn()
    try:
        result = _invoke_db_tables_isolated(
            pm, conn, logger=logging.getLogger("test")
        )
        assert result == ["b"]
        row_doomed = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='first_doomed'"
        ).fetchone()
        row_ok = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='second_ok'"
        ).fetchone()
        assert row_doomed is None
        assert row_ok is not None
    finally:
        conn.close()


def test_invoke_register_routes_resets_openapi_on_success(fake_entry_points):
    """openapi_schema = None runs in finally — both success and failure paths."""
    import logging
    from gixen.plugins import _invoke_register_routes, load_plugins

    plug = _plugin_module("ok", register_routes=lambda app: None)
    fake_entry_points({"ok": plug})

    class FakeApp:
        openapi_schema = {"cached": "stale"}

    pm = load_plugins()
    app = FakeApp()
    _invoke_register_routes(pm, app, logger=logging.getLogger("test"))
    assert app.openapi_schema is None


def test_invoke_register_routes_resets_openapi_on_failure(fake_entry_points, caplog):
    import logging
    from gixen.plugins import _invoke_register_routes, load_plugins

    def boom(app):
        raise RuntimeError("plugin route registration blew up")

    plug = _plugin_module("boom", register_routes=boom)
    fake_entry_points({"boom": plug})

    class FakeApp:
        openapi_schema = {"cached": "stale"}

    pm = load_plugins()
    app = FakeApp()
    caplog.set_level(logging.ERROR, logger="invoke-test")
    _invoke_register_routes(pm, app, logger=logging.getLogger("invoke-test"))
    assert app.openapi_schema is None
    assert any("register_routes failed" in r.message for r in caplog.records)


def test_collect_dashboard_tabs_flattens_lists(fake_entry_points):
    import logging
    from gixen.plugins import _collect_dashboard_tabs, load_plugins

    def tabs():
        return [{"slug": "alpha"}]

    plug = _plugin_module("tab", register_dashboard_tabs=tabs)
    fake_entry_points({"tab": plug})

    pm = load_plugins()
    result = _collect_dashboard_tabs(pm, logger=logging.getLogger("test"))
    assert result == [{"slug": "alpha"}]


def test_collect_dashboard_tabs_skips_bare_dict(fake_entry_points, caplog):
    """Regression for PER-25 ADV-002: a plugin returning a bare dict instead
    of a list-of-dicts must be skipped with a clear log, not iterated as keys."""
    import logging
    from gixen.plugins import _collect_dashboard_tabs, load_plugins

    def tabs():
        return {"slug": "wrong"}  # plugin author mistake

    plug = _plugin_module("bare", register_dashboard_tabs=tabs)
    fake_entry_points({"bare": plug})

    pm = load_plugins()
    caplog.set_level(logging.ERROR, logger="invoke-test")
    result = _collect_dashboard_tabs(pm, logger=logging.getLogger("invoke-test"))
    assert result == []
    assert any(
        "register_dashboard_tabs returned" in r.message
        and "expected list" in r.message
        for r in caplog.records
    )


def test_helpers_log_under_injected_logger_name(fake_entry_points, caplog):
    """The whole point of the logger injection: records appear under the
    caller's chosen logger name, so server.main's caplog tests catch them."""
    import logging
    from gixen.plugins import _invoke_register_routes, load_plugins

    def boom(app):
        raise RuntimeError("boom")

    plug = _plugin_module("boom_logger", register_routes=boom)
    fake_entry_points({"boom-logger": plug})

    class FakeApp:
        openapi_schema = None

    pm = load_plugins()
    caplog.set_level(logging.ERROR, logger="server.main")
    _invoke_register_routes(pm, FakeApp(), logger=logging.getLogger("server.main"))
    # The error record was emitted under "server.main", not "gixen.plugins".
    server_main_records = [r for r in caplog.records if r.name == "server.main"]
    assert any("register_routes failed" in r.message for r in server_main_records)
