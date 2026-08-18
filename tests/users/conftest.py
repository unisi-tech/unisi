"""
Shared pytest fixtures for the users.py unit tests.

Same philosophy as tests/persist_voice_reloder/conftest.py, and for the same
reason: users.py is architecturally not a pure-function module (screen
loading, message dispatch, and cross-user reflection are central to what it
does), so these tests exercise a *real* User against a small fixture app
under fixtures_app/, rather than faking that away with mocks. Kept as its
own directory/conftest.py (not merged into tests/persist_voice_reloder/)
because the two suites cover different, non-overlapping concerns: this one
is about message dispatch, multi-user fan-out, and screen lifecycle;
persist_voice_reloder is about SQLite-backed persistence, VoiceCom's own
internals, and hot-reload. Where the two would otherwise duplicate coverage
(find_path, register_changed_unit's echo-suppression rules, prepare_result's
raw-shape handling, find_element) this suite deliberately leaves that to
tests/persist_voice_reloder/test_messaging.py and doesn't repeat it.

Layout expected by _app_on_path() below:
    <this dir>/fixtures_app/config.py
    <this dir>/fixtures_app/screens/*.py

Adjust UNISI_ROOT below if these files land somewhere else in your project;
it only needs to point at the directory that CONTAINS the unisi/ package.
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
    """Point sys.path/cwd at fixtures_app for the whole test session --
    see tests/persist_voice_reloder/conftest.py's identical fixture for the
    full rationale (one fixture app per session; per-test isolation comes
    from unique session ids in make_user(), not from separate directories).
    """
    from unisi.users import User

    old_cwd = os.getcwd()
    old_path = list(sys.path)
    os.chdir(FIXTURES_APP)
    sys.path.insert(0, str(FIXTURES_APP))

    # User.screen_registry / _screen_registry_ready are CLASS-level state,
    # shared for the life of the whole pytest PROCESS -- not just this
    # session-scoped fixture's own duration. If some OTHER test directory
    # with its own real fixtures_app (e.g. tests/persist_voice_reloder/)
    # already ran first in this same pytest invocation, the registry is
    # already marked ready and would otherwise never rescan, so User()
    # constructions here would try to load ITS screens from OUR cwd and
    # fail with FileNotFoundError. Forcing a rescan here makes this
    # directory's tests correct regardless of what ran before them in the
    # same session.
    User._screen_registry_ready = False
    User.screen_registry = []

    users_dir = FIXTURES_APP / "users"
    if users_dir.exists():
        shutil.rmtree(users_dir)
    users_dir.mkdir(exist_ok=True)

    yield

    os.chdir(old_cwd)
    sys.path[:] = old_path
    # Symmetric reset on the way out too: whichever OTHER real-fixtures_app
    # test directory happens to run after this one in the same session
    # (order depends on discovery/naming, not guaranteed) must not inherit
    # a registry scanned from OUR fixtures_app either.
    User._screen_registry_ready = False
    User.screen_registry = []


@pytest.fixture(autouse=True)
def _isolate_user_class_and_config_state():
    """
    User.last_user is reassigned unconditionally by every construction, and
    Unishare.sessions / config's attributes are process-lifetime globals a
    test might deliberately mutate (activate_session, monitor's config.share
    check, ...). Snapshot/restore around every test so none of that leaks
    into the next one. Deliberately does NOT touch User.screen_registry /
    _screen_registry_ready -- those are meant to stay populated for the
    whole session (see _app_on_path above); resetting them per-test would
    just force a wasteful rescan and isn't needed for isolation here, since
    no test in this file mutates the registry's *contents* except the ones
    using real_screens_dir below, which manage that themselves.
    """
    from unisi.users import User
    from unisi.common import Unishare
    import config

    last_user_snapshot = User.last_user
    sessions_snapshot = dict(Unishare.sessions)
    config_snapshot = dict(config.__dict__)

    yield

    User.last_user = last_user_snapshot
    Unishare.sessions.clear()
    Unishare.sessions.update(sessions_snapshot)
    config.__dict__.clear()
    config.__dict__.update(config_snapshot)


_session_counter = 0


@pytest.fixture
def make_user(_app_on_path):
    """Factory fixture: make_user(screen=None, session=None) -> a User.

    Mirrors tests/persist_voice_reloder/conftest.py's make_user exactly
    (see there for the full rationale) -- a fresh, never-reused session id
    by default (persistence genuinely enabled), or pass an explicit
    `session` to get a second User sharing the same on-disk state as an
    earlier one. Every session touched gets its .db/-shm/-wal files removed
    at the end of the test.
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


@pytest.fixture
def wire_send():
    """Factory fixture: wire_send(user) -> an async send(res, persist=True)
    coroutine function wired onto user.send, exactly like
    tests/persist_voice_reloder/conftest.py's fixture of the same name --
    see there for the full rationale.
    """

    def _wire(user):
        sent = []

        async def send(res, persist=True):
            if isinstance(res, str):
                sent.append(res)
                return res
            res = user.prepare_result(res, persist=persist)
            sent.append(res)
            return res

        user.send = send
        send.sent = sent
        return send

    return _wire


@pytest.fixture
def deliver(wire_send):
    """Factory fixture: deliver(user, block, element, event, value=None) ->
    (result, sent) -- one full request/response cycle, mirroring
    server.py's websocket_handler loop. See
    tests/persist_voice_reloder/conftest.py's identical fixture.
    """
    from unisi.common import ReceivedMessage

    async def _deliver(user, block, element, event, value=None, persist=True):
        if not getattr(user, "send", None):
            wire_send(user)
        msg = ReceivedMessage(
            {"block": block, "element": element, "event": event, "value": value}
        )
        result = await user.result4message(msg)
        sent = await user.send(result, persist=persist)
        return result, sent

    return _deliver


@pytest.fixture
def real_screens_dir(tmp_path, monkeypatch):
    """
    For the handful of tests that need to compile screens from a directory
    OTHER than fixtures_app/ -- User.compile_screen()'s own testing-mode
    validation-skip behaviour (needs a screen with a validation error,
    which shouldn't live in the shared fixtures_app since check_module
    would flag it on every OTHER test's session-scoped load too), and
    User.init_user()'s whole-registry preparation (needs to control exactly
    which screens exist and observe every one of them getting prepared).

    Temporarily chdir's into a fresh tmp_path with its own empty screens/
    subdirectory and resets the CLASS-level registry cache so the next
    User() construction rescans THIS directory -- then restores the
    original cwd, sys.path, and registry state afterwards, since
    _app_on_path's session-scoped fixtures_app setup needs to still be
    intact for every test that runs after this one.
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
