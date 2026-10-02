"""
reloader.drop_dependents: a changed app module outside screens/blocks unloads
itself and, transitively, everything importing it -- shared modules, the user's
private blocks, other loaded screens -- and reports whether the current screen
must be reloaded. (The rest of reloader.py starts a real watchdog Observer at
import time when hot_reload is on and stays untested -- see conftest.py.)
"""
import sys
import types

import pytest

from unisi import reloader


class FakeUser:
    def __init__(self, blocks, screens, current):
        self.modules, self.screens, self.screen_module = blocks, screens, current
        self.dropped = []

    def _drop_private_module(self, name):
        self.modules.pop(name)
        self.dropped.append(name)

    def _remove_module(self, name):
        sys.modules.pop(name, None)


def module(tmp_path, name, source):
    path = tmp_path / f"{name.replace('.', '_')}.py"
    path.write_text(source)
    m = types.ModuleType(name)
    m.__file__ = str(path)
    return m


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(reloader, "app_dir", str(tmp_path))
    names = []

    def shared(name, source):
        m = module(tmp_path, name, source)
        monkeypatch.setitem(sys.modules, name, m)
        names.append(name)
        return m
    yield tmp_path, shared
    for n in names:
        sys.modules.pop(n, None)


def test_transitive_dependents_are_unloaded(app):
    tmp, shared = app
    shared("gen_x", "import re\n")
    shared("img_x", "from gen_x import RenderError\n")
    shared("builder_x", "import os, img_x\n")
    shared("other_x", "import json\n")
    block = module(tmp, "blocks.content", "def f():\n    import builder_x as b\n")
    lone = module(tmp, "blocks.lone", "x = 1\n")
    current = module(tmp, "book", "from blocks.content import *\n")
    side = module(tmp, "side", "import blocks.content as bc\n")
    calm = module(tmp, "calm", "from blocks.lone import x\n")
    user = FakeUser({"blocks.content": block, "blocks.lone": lone}, [current, side, calm], current)

    assert reloader.drop_dependents(user, "gen_x") is True
    assert not {"gen_x", "img_x", "builder_x"} & set(sys.modules) and "other_x" in sys.modules
    assert user.dropped == ["blocks.content"]
    assert user.screens == [current, calm]          # side screen evicted, current reloaded by caller


def test_unloaded_or_unrelated_module_changes_nothing(app):
    tmp, shared = app
    shared("lib_y", "x = 1\n")
    current = module(tmp, "book", "import json\n")
    user = FakeUser({}, [current], current)
    assert reloader.drop_dependents(user, "test_something") is False     # never imported
    assert reloader.drop_dependents(user, "lib_y") is False              # screen does not use it
    assert "lib_y" not in sys.modules                                    # but is reloaded on next import


def test_imports_matching():
    src = "from a.b import c\nimport x as y\nimport p, q.r\n    import lazy\nfrom ab import z\n"
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src)
    try:
        hit = lambda n: reloader.imports(f.name, [n])
        assert all(map(hit, ["a.b", "x", "p", "q.r", "lazy", "ab"]))
        assert not any(map(hit, ["a", "b", "y", "z", "q.r.s"]))
    finally:
        os.unlink(f.name)


def test_app_path_survives_a_borrowed_cwd(tmp_path, monkeypatch):
    """After server start, app paths stay in the app dir even while a library (WanGP)
    has chdir'ed the whole process; before start they are relative, as they always were."""
    from unisi import utils
    monkeypatch.setattr(utils, "_app_root", None)
    assert utils.app_path("screens", "book.py") == "screens/book.py".replace("/", utils.divpath)
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.chdir(app)
    utils.fix_app_root()
    monkeypatch.chdir(tmp_path)                      # the library's folder
    assert utils.app_path("users", "s.db") == str(app / "users" / "s.db")
