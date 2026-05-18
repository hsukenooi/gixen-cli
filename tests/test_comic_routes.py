"""Focused assertions on the comic router extracted in PER-26 Unit 2.

These tests pin two structural invariants:

1. ``server/comic_routes.py`` has zero import-time side effects so it can be
   imported without env vars being set (the late-import pattern that
   ``tests/test_plugin_integration.py`` relies on).
2. The comic router has exactly 5 routes — the 5 comic-dedicated endpoints
   enumerated in the PER-26 plan inventory. If a future change adds or
   removes a route from the comic router, this test fails loudly.

Functional coverage (responses, status codes) is provided by
``tests/test_server_api.py`` against the full mounted app and stays correct
across the extraction.
"""


def test_comic_routes_module_has_zero_import_side_effects(monkeypatch):
    """Importing server.comic_routes without env vars (DB_PATH, GIXEN_USERNAME,
    GIXEN_PASSWORD) must succeed. This guards the late-import pattern used by
    other tests."""
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("GIXEN_USERNAME", raising=False)
    monkeypatch.delenv("GIXEN_PASSWORD", raising=False)
    import importlib
    import server.comic_routes
    importlib.reload(server.comic_routes)  # exercise the import path with no env


def test_comic_router_has_five_routes():
    from server.comic_routes import router

    assert len(router.routes) == 5, (
        f"Expected 5 comic-dedicated routes; got {len(router.routes)}. "
        "If you added or removed a comic route, update this assertion AND "
        "the PER-26 plan inventory."
    )


def test_comic_router_paths_match_plan_inventory():
    from server.comic_routes import router

    expected = {
        ("GET", "/v2/comics"),
        ("GET", "/api/comics"),
        ("POST", "/api/comics"),
        ("POST", "/api/bids/{item_id}/comics/locg"),
        ("POST", "/api/extract-comics"),
    }
    actual = set()
    for r in router.routes:
        for method in r.methods:
            actual.add((method, r.path))
    assert actual == expected
