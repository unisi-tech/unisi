"""
Shared pytest fixtures for the modules.py (ModulesMixin) unit tests.

Same real-User, real-fixture-app philosophy as tests/users/conftest.py and
tests/persist_voice_reloder/conftest.py -- see either for the full
rationale. This directory covers ModulesMixin's own concerns specifically:
the screen registry (build/lookup/upsert/remove), screen compilation and
lazy loading (compile_screen, load_screen, load_lazy, ensure_screen), and
the per-user "private blocks" sys.modules juggling (_install_modules /
_capture_modules / _drop_private_module / set_clean) -- none of which
tests/users/ or tests/persist_voice_reloder/ exercise directly (they call
ensure_screen/compile_screen incidentally, as a means to get a User onto a
screen, but don't test those methods' own branches).

reloader.py is deliberately NOT covered anywhere in this suite: its whole
module body is gated behind `if not config.hot_reload: ... else: <imports
watchdog, starts a real Observer() thread watching the filesystem>` at
IMPORT time, which makes it fundamentally unit-test-unfriendly (no seam to
mock the observer without importing and starting it for real first) --
that's presumably also why no test_reloader.py exists in
tests/persist_voice_reloder/ despite the directory's name.

Layout expected by _app_on_path() below:
    <this dir>/fixtures_app/config.py
    <this dir>/fixtures_app/screens/*.py
    <this dir>/fixtures_app/blocks/*.py
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).parent
FIXTURES_APP = THIS_DIR / "fixtures_app"

UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _app_on_path():
    """Point sys.path/cwd at fixtures_app for the whole test session.

    See tests/users/conftest.py's identical fixture for the full
    rationale, including why User.screen_registry / _screen_registry_ready
    (CLASS-level, shared for the whole pytest PROCESS) are force-reset on
    both entry and exit here -- without that, whichever real-fixtures_app
    test directory happens to run first in a given pytest invocation
    "poisons" the registry for every other one.
    """
    from unisi.users import User

    old_cwd = os.getcwd()
    old_path = list(sys.path)
    os.chdir(FIXTURES_APP)
    sys.path.insert(0, str(FIXTURES_APP))

    User._screen_registry_ready = False
    User.screen_registry = []

    users_dir = FIXTURES_APP / "users"
    if users_dir.exists():
        shutil.rmtree(users_dir)
    users_dir.mkdir(exist_ok=True)

    yield

    os.chdir(old_cwd)
    sys.path[:] = old_path
    User._screen_registry_ready = False
    User.screen_registry = []


@pytest.fixture(autouse=True)
def _isolate_user_class_and_module_state():
    """
    User.last_user, config's attributes, and sys.modules entries under
    blocks_dir ('blocks.*') are process-lifetime globals a test might
    deliberately mutate (that's exactly what _install_modules/
    _capture_modules/_drop_private_module do). Snapshot/restore around
    every test so none of that leaks into the next one. Deliberately does
    NOT touch User.screen_registry / _screen_registry_ready here (see
    _app_on_path above) -- those are meant to stay populated for the whole
    session; individual tests that need to mutate the registry's contents
    (_upsert_screen_info, _remove_screen_info) restore it themselves via
    the registry_snapshot fixture below.
    """
    from unisi.users import User
    from unisi.containers import Screen
    import config

    last_user_snapshot = User.last_user
    toolbar_snapshot = list(User.toolbar)
    config_snapshot = dict(config.__dict__)
    blocks_modules_snapshot = {
        name: mod for name, mod in sys.modules.items() if name.startswith("blocks.")
    }
    # Screen.defaults (unisi/utils.py) is a SINGLE dict built once at import
    # time; its 'toolbar'/'blocks' list VALUES are shared, mutable objects
    # that compile_screen() mutates IN PLACE (`screen.toolbar += ...`) for
    # every screen that doesn't declare its own -- see TestScreenDefaults*
    # below, which deliberately exercises that. Snapshot/restore the
    # CONTENTS of every list value (not just the dict's own key bindings)
    # so that mutation can't leak into whatever test or directory runs next.
    screen_defaults_snapshot = {
        k: (list(v) if isinstance(v, list) else v) for k, v in Screen.defaults.items()
    }

    yield

    User.last_user = last_user_snapshot
    User.toolbar = toolbar_snapshot
    config.__dict__.clear()
    config.__dict__.update(config_snapshot)
    for name in [n for n in sys.modules if n.startswith("blocks.")]:
        if name not in blocks_modules_snapshot:
            del sys.modules[name]
    sys.modules.update(blocks_modules_snapshot)
    for k, v in screen_defaults_snapshot.items():
        current = Screen.defaults.get(k)
        if isinstance(current, list) and isinstance(v, list):
            current[:] = v  # restore CONTENTS in place -- some already-compiled
        else:               # screen might hold a reference to this exact object
            Screen.defaults[k] = v


@pytest.fixture
def registry_snapshot():
    """
    For tests that call _upsert_screen_info/_remove_screen_info directly
    (mutating the CLASS-level registry deliberately, not just through
    ordinary screen loading) -- snapshot/restore User.screen_registry
    around the test so a mutation doesn't leak into whatever test runs
    next in this same session.
    """
    from unisi.users import User

    snapshot = list(User.screen_registry)
    yield
    User.screen_registry = snapshot


@pytest.fixture
def real_screens_dir_modules(tmp_path, monkeypatch):
    """
    For the rare test that needs a screen file OTHER than what's in
    fixtures_app/ (e.g. one with a deliberately malformed `blocks` value).
    Temporarily chdir's into a fresh tmp_path with its own empty screens/
    subdirectory and resets the CLASS-level registry cache, then restores
    the original cwd/sys.path/registry afterwards -- see
    tests/users/conftest.py's identical real_screens_dir for the full
    rationale.
    """
    from unisi.users import User

    screens = tmp_path / "screens"
    screens.mkdir()

    old_cwd = os.getcwd()
    old_registry = list(User.screen_registry)
    old_ready = User._screen_registry_ready

    monkeypatch.chdir(tmp_path)
    User._screen_registry_ready = False

    yield screens

    os.chdir(old_cwd)
    User.screen_registry = old_registry
    User._screen_registry_ready = old_ready


_session_counter = 0


@pytest.fixture
def make_user():
    """Factory fixture: make_user(screen=None, session=None) -> a User.

    Same pattern as tests/users/conftest.py's make_user -- a fresh,
    never-reused session id by default, or pass an explicit `session` to
    share on-disk state with an earlier User. Every session touched gets
    its .db/-shm/-wal files removed at the end of the test.
    """
    from unisi.users import User

    global _session_counter
    sessions_used = set()

    def _make(screen=None, session=None):
        global _session_counter
        if session is None:
            _session_counter += 1
            session = f"pytest_{os.getpid()}_{_session_counter}"
        sessions_used.add(session)
        user = User(session)
        if screen:
            module = user.ensure_screen(screen)
            user.screen_module = module
        return user

    yield _make

    for session in sessions_used:
        base = FIXTURES_APP / "users" / f"{session}.db"
        for suffix in ("", "-shm", "-wal"):
            p = Path(str(base) + suffix)
            if p.exists():
                p.unlink()
