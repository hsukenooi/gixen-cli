import sys
import types
from importlib.metadata import EntryPoint

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: tests that hit real Gixen")


@pytest.fixture
def fake_entry_points(monkeypatch):
    """Inject synthetic entry points into gixen.plugins.entry_points.

    Usage:

        def test_x(fake_entry_points):
            mod = types.ModuleType("fake_plugin")
            mod.register_routes = hookimpl(lambda app: None)
            fake_entry_points({"my-plugin": mod})

    The factory accepts an ordered dict of {name: plugin_module}. It
    registers each module in sys.modules so the loader can resolve it
    via the standard EntryPoint.load() path, then monkeypatches
    gixen.plugins.entry_points to return EntryPoints pointing at them.
    """

    def factory(plugins: dict[str, types.ModuleType]):
        eps = []
        for name, mod in plugins.items():
            module_name = f"_test_fake_{name.replace('-', '_')}"
            sys.modules[module_name] = mod
            eps.append(EntryPoint(name=name, value=module_name, group="gixen.plugins"))

        def fake(group: str):
            return eps if group == "gixen.plugins" else []

        monkeypatch.setattr("gixen.plugins.entry_points", fake)
        return eps

    return factory
