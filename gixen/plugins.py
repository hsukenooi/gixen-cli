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

import pluggy

__all__ = [
    "hookimpl",
    "hookspec",
    "GixenPluginSpec",
    "make_plugin_manager",
    "load_plugins",
]


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
    """Stub — Unit 3 will implement entry-point discovery and isolation."""
    raise NotImplementedError("Unit 3 implements this.")
