from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

from email_concierge.extractors.discovery import discover_plugins


def _write_plugin_pkg(tmp_path: Path, files: dict[str, str]) -> str:
    """Create a throwaway plugin package under tmp_path. Returns the
    dotted package name (caller supplies it via the `package` kwarg to
    discover_plugins after importing).
    """
    pkg_name = f"_fake_plugins_{tmp_path.name.replace('-', '_')}"
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for filename, body in files.items():
        (pkg_dir / filename).write_text(textwrap.dedent(body))
    return pkg_name


@pytest.fixture
def plugin_pkg(tmp_path, monkeypatch):
    """Returns a factory that materializes a fake plugin package and
    returns the imported package module.
    """

    def _make(files: dict[str, str]):
        pkg_name = _write_plugin_pkg(tmp_path, files)
        monkeypatch.syspath_prepend(str(tmp_path))
        # Ensure a fresh import (pytest reruns can reuse interpreter state).
        for mod in list(sys.modules):
            if mod == pkg_name or mod.startswith(pkg_name + "."):
                del sys.modules[mod]
        return importlib.import_module(pkg_name)

    return _make


GOOD_A = """
    class _Base:  # should be skipped (underscore-prefixed)
        name = "base"
        stage = 2

    class GoodA:
        name = "good_a"
        stage = 2
        priority = 10

        def can_handle(self, email):
            return 1.0

        def extract(self, email):
            return None
"""

GOOD_B = """
    class GoodB:
        name = "good_b"
        stage = 2
        priority = 5

        def can_handle(self, email):
            return 1.0

        def extract(self, email):
            return None
"""

BROKEN = """
    raise RuntimeError("intentional import failure")
"""

NO_PRIORITY = """
    class NoPriority:
        name = "no_priority"
        stage = 3
        # no `priority` attribute; discovery must default to 0

        def can_handle(self, email):
            return 1.0

        def extract(self, email):
            return None
"""


def test_discovers_valid_plugins(plugin_pkg):
    pkg = plugin_pkg({"good_a.py": GOOD_A, "good_b.py": GOOD_B})
    plugins = discover_plugins(package=pkg, disabled=set())
    names = [p.name for p in plugins]
    assert names == ["good_b", "good_a"]  # sorted by (stage=2, priority asc)


def test_broken_plugin_does_not_block_others(plugin_pkg, caplog):
    pkg = plugin_pkg({"good_a.py": GOOD_A, "broken.py": BROKEN})
    plugins = discover_plugins(package=pkg, disabled=set())
    assert [p.name for p in plugins] == ["good_a"]


def test_priority_defaults_to_zero(plugin_pkg):
    pkg = plugin_pkg({"no_priority.py": NO_PRIORITY})
    plugins = discover_plugins(package=pkg, disabled=set())
    assert len(plugins) == 1
    assert getattr(plugins[0], "priority", 0) == 0


def test_sorted_by_stage_then_priority(plugin_pkg):
    pkg = plugin_pkg(
        {
            "good_a.py": GOOD_A,
            "good_b.py": GOOD_B,
            "no_priority.py": NO_PRIORITY,
        }
    )
    plugins = discover_plugins(package=pkg, disabled=set())
    stage_priority = [(p.stage, getattr(p, "priority", 0)) for p in plugins]
    assert stage_priority == sorted(stage_priority)


def test_disabled_plugins_excluded(plugin_pkg):
    pkg = plugin_pkg({"good_a.py": GOOD_A, "good_b.py": GOOD_B})
    plugins = discover_plugins(package=pkg, disabled={"good_a"})
    assert [p.name for p in plugins] == ["good_b"]


def test_skips_underscore_modules(plugin_pkg, tmp_path):
    pkg = plugin_pkg(
        {
            "_helper.py": "GOOD_MARKER = 1\n",
            "good_a.py": GOOD_A,
        }
    )
    plugins = discover_plugins(package=pkg, disabled=set())
    assert [p.name for p in plugins] == ["good_a"]


def test_skips_reexported_classes(plugin_pkg):
    # A module that re-exports GoodA from another module: discovery should
    # attribute the class only to its defining module, preventing double-count.
    reexport = """
        from .good_a import GoodA  # re-export; must not be re-registered
    """
    pkg = plugin_pkg({"good_a.py": GOOD_A, "reexport.py": reexport})
    plugins = discover_plugins(package=pkg, disabled=set())
    assert [p.name for p in plugins] == ["good_a"]
