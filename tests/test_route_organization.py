"""Route-organization regression tests.

PER-26 Unit 4. FastAPI silently accepts duplicate (method, path) pairs and
applies last-write-wins — there is no warning. After PER-26 starts using
``app.include_router`` for the comic routes, future plugins (PER-30 and
beyond) could accidentally re-register the same paths. This test catches
the class of bug at lifespan-startup time.

The check excludes FastAPI's auto-generated /openapi.json /docs /redoc
/docs/oauth2-redirect routes; they appear once and we don't own them.
"""
from __future__ import annotations

import sys
import types
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


_AUTO_PATHS = {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def _install_plugins(monkeypatch, plugins: dict[str, types.ModuleType]):
    eps = []
    for name, mod in plugins.items():
        module_name = f"_test_routeorg_{name.replace('-', '_')}"
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
def app_no_plugins(tmp_path, monkeypatch):
    """Bring up the server app with no plugins installed."""
    _install_plugins(monkeypatch, {})
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    with patch("server.main.GixenClient", return_value=_mock_gixen()):
        from server.main import app
        with TestClient(app) as client:
            yield client.app


def test_no_duplicate_method_path_pairs(app_no_plugins):
    """No two routes in app.routes share the same (method, path).

    Catches silent shadowing — e.g., a future include_router(...) call that
    re-registers /api/comics on top of the existing comic router, or a
    handler accidentally decorated twice. FastAPI never warns; this test
    does."""
    pairs: list[tuple[str, str]] = []
    for route in app_no_plugins.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        if path in _AUTO_PATHS:
            continue
        for method in methods:
            pairs.append((method, path))

    duplicates = [p for p in pairs if pairs.count(p) > 1]
    assert not duplicates, (
        f"Duplicate (method, path) pairs in app.routes: {sorted(set(duplicates))}. "
        "FastAPI applies last-write-wins silently; this is almost always a bug."
    )
