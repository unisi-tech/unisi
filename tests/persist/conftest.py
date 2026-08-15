"""
Shared pytest fixtures for the persist.py / users.py unit tests.

These tests exercise a *real* User against a *real* (throwaway, per-test)
SQLite file and a small fixture app under fixtures_app/ -- persist.py and
users.py are architecturally not pure functions (screen loading, tree
position, and SQLite I/O are central to what they do), so faking all of that
with mocks would mostly end up re-testing the mocks. Pure, dependency-free
helpers (see test_helpers.py) ARE tested as plain unit tests with no fixture
app involved at all.

Layout expected by _app_on_path() below:
    <this dir>/fixtures_app/config.py
    <this dir>/fixtures_app/screens/*.py
    <this dir>/fixtures_app/blocks/*.py

Adjust UNISI_ROOT below to wherever your `unisi` package actually lives
relative to this conftest.py -- it only needs to be on sys.path.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).parent
FIXTURES_APP = THIS_DIR / "fixtures_app"

# --- Make the `unisi` package importable ------------------------------------
# Change this if these test files land somewhere else in your project; it
# just needs to point at the directory that CONTAINS the unisi/ package.
# Default assumes this file lives at <repo_root>/tests/unit/conftest.py.
UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _app_on_path():
    """Point sys.path/cwd at fixtures_app for the whole test session, the
    same way a real process running unisi.start() from that directory would
    -- config.py and the screens/blocks packages are resolved off of this.
    Session-scoped and autouse: every test in this directory needs it, and
    unisi's module system (ModulesMixin) keys screens/blocks modules into
    sys.modules by dotted name, so juggling multiple different app
    directories within one process is asking for cross-test collisions --
    one fixture app for the whole run, one fresh session id per test (see
    make_user below) for isolation instead.
    """
    old_cwd = os.getcwd()
    old_path = list(sys.path)
    os.chdir(FIXTURES_APP)
    sys.path.insert(0, str(FIXTURES_APP))

    users_dir = FIXTURES_APP / "users"
    if users_dir.exists():
        shutil.rmtree(users_dir)
    users_dir.mkdir(exist_ok=True)

    yield

    os.chdir(old_cwd)
    sys.path[:] = old_path


_session_counter = 0


@pytest.fixture
def make_user(_app_on_path):
    """Factory fixture: make_user(screen=None, session=None) -> a User.

    With no `session`, each call gets its own never-reused id (so no two
    tests, and no two calls within one test, ever share a SQLite file or
    in-memory identity cache) and persistence is actually enabled (`session
    == 'autotest'` -- the testdir sentinel -- is what disables it; see
    User.testing / _persist_enabled).

    Pass an explicit `session` to get a *second* User backed by the *same*
    SQLite file as an earlier one -- the way to simulate "the user reloads
    the page" / "reconnects" and check that persisted state actually comes
    back, since a single User instance restores once at screen-load time
    and won't naturally show you that without a fresh instance to load into.

    Every session this fixture touches (auto-generated or explicit) has its
    .db/-shm/-wal files removed at the end of the test, so a failed
    assertion never leaves stray state for a later test to trip over.
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
    coroutine function, wired onto user.send exactly the way server.py's own
    websocket_handler wires its send closure onto a live connection (same
    prepare_result call, same default persist=True). Needed for anything
    that goes through prepare_result at all, and specifically for
    progress()/dialog-close, which call self.send(...) internally and raise
    AttributeError if nothing ever set it (there's no real socket in these
    tests -- collected messages just accumulate in send.sent instead of
    going out over a wire).
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
    (result, sent) for one full request/response cycle -- result4message()
    then the real send(result) -- exactly mirroring one iteration of
    server.py's websocket_handler loop. Wires send onto `user` itself if
    nothing already did (via wire_send), so a test can just call this
    directly without a separate setup step for the common case.
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
