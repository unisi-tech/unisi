"""
Shared pytest fixtures for the server.py / utils.py / common.py unit tests.

These three modules are the framework's own foundation -- common.py has the
handler-composition/message primitives everything else is built from,
utils.py owns process bootstrap (config loading, Screen.defaults, logging)
plus a handful of small path/url helpers, and server.py wires all of it
into an actual aiohttp application (make_user, the websocket protocol loop,
static/upload serving, and start()'s route setup).

Test-design split, mirroring tests/db_units's "pure vs. architectural"
philosophy:

  * common.py is almost entirely pure functions/small classes (flatten,
    compose_handlers, ArgObject, Message, ...) with no dependency on a
    running app -- test_common.py exercises those directly, no fixtures_app
    needed for most of it.
  * utils.py is mostly pure path/url helpers too, but a few things
    (upload_path, Screen.defaults, the config-bootstrap block itself) are
    genuinely dependent on the ambient `config` module -- test_utils.py
    uses monkeypatch on the live `config` object for those, and a couple of
    *subprocess*-isolated tests (see test_utils.py's own docstring) for the
    import-time bootstrap block, since that only ever runs once per
    process and every other test in the whole suite needs it to have
    already run.
  * server.py is architecturally an aiohttp app: make_user/websocket_handler/
    post_handler/static_serve only make sense wired into a real
    aiohttp.web.Application and exercised with aiohttp's own test client,
    the same "real objects over mocks" philosophy tests/users and
    tests/persist_voice_reloder already use for User itself. test_server.py
    uses the `app_client` fixture below for exactly that.

Layout expected by _app_on_path() below:
    <this dir>/fixtures_app/config.py
    <this dir>/fixtures_app/screens/*.py

Adjust UNISI_ROOT below if these files land somewhere else in your project;
it only needs to point at the directory that CONTAINS the unisi/ package.
"""
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

THIS_DIR = Path(__file__).parent
FIXTURES_APP = THIS_DIR / "fixtures_app"

UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _app_on_path():
    """Point sys.path/cwd at fixtures_app for the whole test session -- see
    tests/persist_voice_reloder/conftest.py's identical fixture for the full
    rationale (one fixture app per session; per-test isolation comes from
    unique session ids in new_user(), not from separate directories).
    """
    from unisi.users import User

    old_cwd = os.getcwd()
    old_path = list(sys.path)
    os.chdir(FIXTURES_APP)
    sys.path.insert(0, str(FIXTURES_APP))

    # User.screen_registry / _screen_registry_ready are CLASS-level state,
    # shared for the life of the whole pytest PROCESS -- not just this
    # session-scoped fixture's own duration. If some OTHER test directory
    # with its own real fixtures_app already ran first in this same pytest
    # invocation, the registry is already marked ready and would otherwise
    # never rescan, so User() constructions here would try to load ITS
    # screens from OUR cwd and fail with FileNotFoundError. Forcing a
    # rescan here makes this directory's tests correct regardless of what
    # ran before them in the same session.
    User._screen_registry_ready = False
    User.screen_registry = []

    downloads_dir = FIXTURES_APP / "downloads"
    downloads_dir.mkdir(exist_ok=True)
    users_dir = FIXTURES_APP / "users"
    users_dir.mkdir(exist_ok=True)

    yield

    os.chdir(old_cwd)
    sys.path[:] = old_path
    User._screen_registry_ready = False
    User.screen_registry = []


@pytest.fixture(autouse=True)
def _isolate_core_state():
    """
    User.last_user/User.count, Unishare.sessions/Unishare.test_list, and
    config's attributes are process-lifetime globals that make_user(),
    handle(), and test() (all in server.py) mutate directly. Snapshot/
    restore around every test so nothing leaks into the next one -- same
    discipline as tests/users/conftest.py's
    _isolate_user_class_and_config_state, extended to the extra globals
    server.py itself owns (Unishare.test_list, User.count).
    """
    from unisi.users import User
    from unisi.common import Unishare
    import config

    last_user_snapshot = User.last_user
    count_snapshot = User.count
    sessions_snapshot = dict(Unishare.sessions)
    test_list_snapshot = list(Unishare.test_list)
    config_snapshot = dict(config.__dict__)

    yield

    User.last_user = last_user_snapshot
    User.count = count_snapshot
    Unishare.sessions.clear()
    Unishare.sessions.update(sessions_snapshot)
    Unishare.test_list[:] = test_list_snapshot
    config.__dict__.clear()
    config.__dict__.update(config_snapshot)


_session_counter = 0


@pytest.fixture
def new_user(_app_on_path):
    """Factory fixture: new_user(screen=None, session=None) -> a User.

    Named new_user (not make_user) to avoid colliding with
    unisi.server.make_user -- the actual function under test in
    test_server.py, which wraps this same User(...) construction with
    session-parsing/sharing/mirroring logic. Otherwise mirrors
    tests/users/conftest.py's make_user exactly: a fresh, never-reused
    session id by default, or pass an explicit `session` for a second User
    sharing the same on-disk state as an earlier one.
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
        user = User(session, screen=screen)
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
    coroutine function wired onto user.send -- see
    tests/persist_voice_reloder/conftest.py's identical fixture for the
    full rationale. Used by tests that call result4message()/eval_handler
    paths needing self.send to already exist, without going through a real
    websocket (app_client below is for the tests that specifically want
    that).
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


@pytest_asyncio.fixture
async def app_client(_app_on_path):
    """A real aiohttp TestClient wired to server.py's actual routes -- the
    same web.get('/ws', ...)/web.static(...)/web.get('/{tail:.*}', ...)/
    web.post('/', ...) that start() itself registers, minus the
    web.run_app(...) call (which blocks forever listening on a real port
    and is explicitly out of scope for a unit test -- start()'s own tests
    monkeypatch it out and assert on the route list instead).

    Built directly on aiohttp.test_utils.TestServer/TestClient (aiohttp is
    already a hard dependency; pytest-aiohttp is not, so this avoids adding
    one just for a fixture) rather than hand-rolling request/response
    objects -- for the same reason tests/users and tests/persist_voice_reloder
    use a real User instead of a mock: make_user/websocket_handler/
    post_handler/static_serve are aiohttp handlers first and foremost, and
    the websocket protocol handshake in particular isn't something worth
    faking by hand.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    import unisi.server as server_mod
    import config

    app = web.Application()
    app.add_routes([
        web.get('/ws', server_mod.websocket_handler),
        web.static(f'/{config.upload_dir}', config.upload_dir),
        web.get('/{tail:.*}', server_mod.static_serve),
        web.post('/', server_mod.post_handler),
    ])
    async with TestClient(TestServer(app)) as client:
        yield client
