"""Plugin entry-point system for gixen-cli.

External packages register against the ``gixen.plugins`` entry-point group.
At FastAPI startup, the loader (``load_plugins``) discovers installed
plugins and registers them with a ``pluggy.PluginManager``. The host then
invokes three hooks during the lifespan:

    register_db_tables(conn)        — plugin creates its own SQLite tables
    register_routes(app)            — plugin mounts FastAPI routes on the app
    register_dashboard_tabs() -> list[dict]
                                    — plugin returns dashboard tab specs

Plugin authors import the ``hookimpl`` marker from this module to decorate
their hook implementations::

    from gixen.plugins import hookimpl

    @hookimpl
    def register_routes(app):
        app.include_router(...)
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points

import pluggy

__all__ = [
    "hookimpl",
    "hookspec",
    "GixenPluginSpec",
    "make_plugin_manager",
    "load_plugins",
]

_logger = logging.getLogger("gixen.plugins")


hookspec = pluggy.HookspecMarker("gixen")
hookimpl = pluggy.HookimplMarker("gixen")


class GixenPluginSpec:
    """The contract every gixen plugin can implement.

    A plugin does not have to implement all three hooks — pluggy will only
    fire the hooks the plugin has decorated. Hookspecs document the
    signature and ordering contract; hook ordering is by entry-point name
    (alphabetical) unless a plugin uses ``@hookimpl(tryfirst=True)`` or
    ``trylast=True`` to override.

    Error handling: per-plugin isolation is applied at hook-invocation time
    by the host's lifespan. A plugin whose hook raises will not prevent
    other plugins from registering (for ``register_db_tables`` — see
    ``load_plugins`` and the lifespan in ``server/main.py``).
    """

    @hookspec
    def register_routes(self, app):
        """Register FastAPI routes on the host application.

        :param app: the host FastAPI instance. Plugins typically build an
            ``APIRouter`` and call ``app.include_router(router, prefix=...)``.
        """

    @hookspec
    def register_db_tables(self, conn):
        """Create plugin-owned SQLite tables.

        :param conn: the host's open ``sqlite3.Connection``. Plugins should
            use ``CREATE TABLE IF NOT EXISTS`` so re-runs are idempotent.
            Tables must be namespaced (e.g. ``comic_fmv``, not ``fmv``) to
            avoid collisions with the core ``bids`` table or other plugins.

        DDL executed in this hook is wrapped in a SQLite savepoint by the
        host; a failure rolls back this plugin's DDL only.
        """

    @hookspec
    def register_dashboard_tabs(self) -> list[dict]:
        """Return a list of dashboard tab specifications.

        Each spec is a plain ``dict``. The exact shape is defined by PER-28
        (which builds the dashboard renderer) — for PER-25, plugins return
        free-form dicts that the host stores on ``app.state.dashboard_tabs``
        without consuming them.
        """


def make_plugin_manager() -> pluggy.PluginManager:
    """Construct a fresh ``PluginManager`` with the gixen hookspecs loaded.

    Used by ``load_plugins`` and by tests that want to register fake
    plugins directly via ``pm.register(...)`` without going through the
    entry-point discovery path.
    """
    pm = pluggy.PluginManager("gixen")
    pm.add_hookspecs(GixenPluginSpec)
    return pm


def load_plugins(group: str = "gixen.plugins") -> pluggy.PluginManager:
    """Discover and register all plugins declared under the entry-point group.

    Plugins are registered in deterministic order — sorted by entry-point
    name — so that hook invocation order is reproducible across machines
    (the default order from ``entry_points()`` is sys.path order, which is
    not stable). Plugins needing explicit ordering can use
    ``@hookimpl(tryfirst=True)`` or ``trylast=True``.

    Per-plugin error isolation: a plugin whose ``ep.load()`` raises, whose
    ``pm.register()`` raises (e.g. duplicate name), or whose registered
    hookimpls reference a misspelled hookspec, is logged at ERROR and
    skipped. The loader always returns a usable ``PluginManager`` — never
    raises on plugin failure.
    """
    pm = make_plugin_manager()
    for ep in sorted(entry_points(group=group), key=lambda e: e.name):
        try:
            plugin = ep.load()
        except Exception:
            _logger.exception(
                "Plugin %s failed to load (from %s)", ep.name, ep.value
            )
            continue
        try:
            pm.register(plugin, name=ep.name)
        except Exception:
            _logger.exception("Plugin %s failed to register", ep.name)

    # Validate that every @hookimpl in registered plugins matches an existing
    # hookspec. Misspelled hook names (e.g. ``register_route`` vs
    # ``register_routes``) raise PluginValidationError here.
    try:
        pm.check_pending()
    except Exception:
        _logger.exception(
            "Plugin validation failed (misspelled or unknown hookspec)"
        )
    return pm
