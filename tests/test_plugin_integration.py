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
        sys.modules[module_name] = mod
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
