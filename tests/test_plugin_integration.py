"""Integration tests for plugin loading via the FastAPI lifespan.

These tests use TestClient as a context manager so the lifespan startup
runs end-to-end: discovery -> registration -> register_db_tables ->
register_routes -> register_dashboard_tabs -> openapi schema regen.
"""
from __future__ import annotations

import logging
import sys
import types
import sqlite3
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient


def _install_plugins(monkeypatch, plugins: dict[str, types.ModuleType]):
    """Wire synthetic plugins into gixen.plugins.entry_points discovery."""
    eps = []
    for name, mod in plugins.items():
        module_name = f"_test_plugin_{name.replace('-', '_')}"
        monkeypatch.setitem(sys.modules, module_name, mod)
        eps.append(EntryPoint(name=name, value=module_name, group="gixen.plugins"))
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: eps if group == "gixen.plugins" else [],
    )


def _mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.purge_completed.return_value = None
    return m


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Builds a TestClient under whatever set of plugins the test installs.

    Plugins must be installed via _install_plugins BEFORE this fixture is
    used — because TestClient(app).__enter__ triggers the lifespan, which
    calls load_plugins() once.

    Returns (client_factory, db_path). Use db_path to open a fresh
    sqlite3.Connection from the test thread when asserting against tables,
    since the server's _db is bound to the lifespan worker thread.
    """
    db_path = tmp_path / "test.db"

    def factory():
        monkeypatch.setenv("DB_PATH", str(db_path))
        monkeypatch.setenv("GIXEN_USERNAME", "testuser")
        monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
        monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
        monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
        with patch("server.main.GixenClient", return_value=_mock_gixen()):
            # Late import so monkeypatched env vars are read in init_db.
            from server.main import app
            return TestClient(app)

    factory.db_path = db_path
    return factory


def _table_exists(db_path, name: str) -> bool:
    """Open a fresh connection and check whether a table exists."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# --- Baseline: no plugins installed --------------------------------------------


def test_no_plugins_lifespan_succeeds(make_app, monkeypatch):
    _install_plugins(monkeypatch, {})
    with make_app() as client:
        # Core route still works.
        r = client.get("/health")
        assert r.status_code == 200
        # Plugin manager is on app.state but empty.
        pm = client.app.state.plugin_manager
        assert pm.list_name_plugin() == []
        assert client.app.state.dashboard_tabs == []


# --- app.state.db lifecycle (PER-26 Unit 1) ------------------------------------


def test_app_state_db_is_set_during_lifespan(make_app, monkeypatch):
    """app.state.db is the same sqlite3.Connection as server.main._db while the
    lifespan is active."""
    _install_plugins(monkeypatch, {})
    with make_app() as client:
        from server import main as server_main
        assert isinstance(client.app.state.db, sqlite3.Connection)
        assert client.app.state.db is server_main._db


def test_app_state_db_cleared_after_teardown(make_app, monkeypatch):
    """After the TestClient context exits, app.state.db is None so callers can't
    accidentally use a closed connection."""
    _install_plugins(monkeypatch, {})
    with make_app() as client:
        captured_app = client.app
        assert captured_app.state.db is not None
    # Outside the context: lifespan teardown ran.
    assert captured_app.state.db is None


def test_plugin_register_db_tables_sees_app_state_db(make_app, monkeypatch):
    """A plugin's register_db_tables hook can read app.state.db and finds the
    same connection it received as the conn parameter."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("state_db_during_ddl_plug")
    captured = {}

    @hookimpl
    def register_db_tables(conn):
        # Stash both connections so the test can compare after lifespan startup.
        from server.main import app as host_app
        captured["conn"] = conn
        captured["app_state_db"] = host_app.state.db

    mod.register_db_tables = register_db_tables
    _install_plugins(monkeypatch, {"statedbplug": mod})

    with make_app() as client:
        assert captured["conn"] is not None
        assert captured["app_state_db"] is captured["conn"]


def test_plugin_route_can_use_app_state_db(make_app, monkeypatch):
    """A plugin's route handler can read app.state.db (captured in closure from
    the register_routes hook) and execute SQL against it. Closure-over-app is
    the simplest plugin pattern; equivalent to request.app.state.db for internal
    use."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("state_db_in_route_plug")

    @hookimpl
    def register_routes(app):
        router = APIRouter()
        host_app = app  # capture for the handler closure

        @router.get("/api/fake/db-ping")
        async def db_ping():
            row = host_app.state.db.execute("SELECT 1").fetchone()
            return {"ok": row[0] == 1}

        app.include_router(router)

    mod.register_routes = register_routes
    _install_plugins(monkeypatch, {"dbpingplug": mod})

    with make_app() as client:
        r = client.get("/api/fake/db-ping")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# --- register_routes -----------------------------------------------------------


def test_plugin_registers_route(make_app, monkeypatch):
    from gixen.plugins import hookimpl

    mod = types.ModuleType("route_plug")

    @hookimpl
    def register_routes(app):
        router = APIRouter()

        @router.get("/api/fake/ping")
        def ping():
            return {"ok": "pong"}

        app.include_router(router)

    mod.register_routes = register_routes
    _install_plugins(monkeypatch, {"routeplug": mod})

    with make_app() as client:
        r = client.get("/api/fake/ping")
        assert r.status_code == 200
        assert r.json() == {"ok": "pong"}


def test_plugin_routes_visible_in_openapi(make_app, monkeypatch):
    from gixen.plugins import hookimpl

    mod = types.ModuleType("openapi_plug")

    @hookimpl
    def register_routes(app):
        router = APIRouter()

        @router.get("/api/fake/openapi-ping")
        def ping():
            return {"ok": True}

        app.include_router(router)

    mod.register_routes = register_routes
    _install_plugins(monkeypatch, {"openapiplug": mod})

    with make_app() as client:
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/api/fake/openapi-ping" in paths


# --- register_db_tables --------------------------------------------------------


def test_plugin_creates_table(make_app, monkeypatch):
    from gixen.plugins import hookimpl

    mod = types.ModuleType("table_plug")

    @hookimpl
    def register_db_tables(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fake_t (id INTEGER PRIMARY KEY)"
        )

    mod.register_db_tables = register_db_tables
    _install_plugins(monkeypatch, {"tableplug": mod})

    with make_app() as client:
        assert _table_exists(make_app.db_path, "fake_t")


def test_plugin_db_tables_failure_rolls_back_savepoint(make_app, monkeypatch, caplog):
    """A plugin whose register_db_tables raises after one statement has its
    savepoint rolled back. The created table must NOT exist."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("bad_table_plug")

    @hookimpl
    def register_db_tables(conn):
        conn.execute("CREATE TABLE bad_t (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated failure mid-DDL")

    mod.register_db_tables = register_db_tables
    _install_plugins(monkeypatch, {"badtableplug": mod})

    caplog.set_level(logging.ERROR, logger="server.main")
    with make_app() as client:
        # Savepoint rolled back — table should NOT exist.
        assert not _table_exists(make_app.db_path, "bad_t")
        # Server still started.
        assert client.get("/health").status_code == 200


def test_plugin_db_tables_failure_does_not_block_other_plugins(make_app, monkeypatch):
    """Plugin A's DDL fails; plugin B's DDL still runs."""
    from gixen.plugins import hookimpl

    mod_a = types.ModuleType("dll_fail")

    @hookimpl
    def register_db_tables_a(conn):
        conn.execute("CREATE TABLE doomed (id INTEGER)")
        raise RuntimeError("nope")

    mod_a.register_db_tables = register_db_tables_a

    mod_b = types.ModuleType("dll_ok")

    @hookimpl
    def register_db_tables_b(conn):
        conn.execute("CREATE TABLE survivor (id INTEGER)")

    mod_b.register_db_tables = register_db_tables_b

    # Names chosen so sort order is a, b.
    _install_plugins(monkeypatch, {"a_failing": mod_a, "b_passing": mod_b})

    with make_app() as client:
        assert not _table_exists(make_app.db_path, "doomed")
        assert _table_exists(make_app.db_path, "survivor")


# --- register_dashboard_tabs ---------------------------------------------------


def test_plugin_dashboard_tabs_collected(make_app, monkeypatch):
    from gixen.plugins import hookimpl

    mod = types.ModuleType("tab_plug")

    @hookimpl
    def register_dashboard_tabs():
        return [{"slug": "fake", "label": "Fake", "key": "f", "route": "/v2/fake"}]

    mod.register_dashboard_tabs = register_dashboard_tabs
    _install_plugins(monkeypatch, {"tabplug": mod})

    with make_app() as client:
        tabs = client.app.state.dashboard_tabs
        assert tabs == [
            {"slug": "fake", "label": "Fake", "key": "f", "route": "/v2/fake"}
        ]


# --- Error paths in the lifespan -----------------------------------------------


def test_plugin_register_routes_raise_does_not_crash_server(make_app, monkeypatch, caplog):
    """If a plugin's register_routes raises, the server still starts and core
    routes still work. The exception is logged at ERROR."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("bad_route_plug")

    @hookimpl
    def register_routes(app):
        raise RuntimeError("plugin route registration blew up")

    mod.register_routes = register_routes
    _install_plugins(monkeypatch, {"badroute": mod})

    caplog.set_level(logging.ERROR, logger="server.main")
    with make_app() as client:
        # Core route still works.
        assert client.get("/health").status_code == 200
    # The exception was logged.
    assert any("register_routes failed" in r.message for r in caplog.records)


def test_plugin_register_dashboard_tabs_raise_falls_back_to_empty(make_app, monkeypatch, caplog):
    """If a plugin's register_dashboard_tabs raises, app.state.dashboard_tabs
    is set to [] and the server still starts."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("bad_tab_plug")

    @hookimpl
    def register_dashboard_tabs():
        raise RuntimeError("tab plugin blew up")

    mod.register_dashboard_tabs = register_dashboard_tabs
    _install_plugins(monkeypatch, {"badtab": mod})

    caplog.set_level(logging.ERROR, logger="server.main")
    with make_app() as client:
        assert client.app.state.dashboard_tabs == []
        assert client.get("/health").status_code == 200
    assert any("register_dashboard_tabs failed" in r.message for r in caplog.records)


def test_plugin_register_dashboard_tabs_bare_dict_is_rejected(make_app, monkeypatch, caplog):
    """A plugin returning a bare dict instead of list-of-dicts must NOT
    produce string-keyed corruption in app.state.dashboard_tabs. The lifespan
    skips the plugin's contribution with a clear log message.

    Regression test for adversarial finding ADV-002: previously a bare dict
    iterated as keys, producing app.state.dashboard_tabs == ['slug', 'label']."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("bare_dict_plug")

    @hookimpl
    def register_dashboard_tabs():
        # Plugin author mistake: a single dict, not a list.
        return {"slug": "wrong", "label": "Wrong"}

    mod.register_dashboard_tabs = register_dashboard_tabs
    _install_plugins(monkeypatch, {"baredict": mod})

    caplog.set_level(logging.ERROR, logger="server.main")
    with make_app() as client:
        tabs = client.app.state.dashboard_tabs
        # The bare dict is skipped entirely, not iterated as keys.
        assert tabs == []
    assert any(
        "register_dashboard_tabs returned" in r.message
        and "expected list" in r.message
        for r in caplog.records
    )


def test_plugin_executescript_does_not_crash_lifespan(make_app, monkeypatch, caplog):
    """A plugin that violates the hookspec (uses conn.executescript instead of
    conn.execute) must not crash the entire server. The savepoint is
    destroyed by executescript's implicit COMMIT, so the rollback attempt
    raises OperationalError — but the lifespan must catch the secondary
    exception and continue.

    Regression test for reliability/correctness/adversarial finding (REL-01,
    COR-01, ADV-001)."""
    from gixen.plugins import hookimpl

    mod = types.ModuleType("executescript_plug")

    @hookimpl
    def register_db_tables(conn):
        # Forbidden per the hookspec docstring — but plugins will make this
        # mistake, and the host must not crash when they do.
        conn.executescript("CREATE TABLE bad_es_t (id INTEGER);")
        raise RuntimeError("plugin raised after executescript")

    mod.register_db_tables = register_db_tables
    _install_plugins(monkeypatch, {"executescript-plug": mod})

    caplog.set_level(logging.ERROR, logger="server.main")
    with make_app() as client:
        # Server still started.
        assert client.get("/health").status_code == 200
    # The savepoint-cleanup failure was logged.
    assert any(
        "Savepoint cleanup failed" in r.message or "conn.executescript" in r.message
        for r in caplog.records
    )
