"""
Shared pytest fixtures for the unisi/autotest.py unit tests.

autotest.py is architecturally similar to server.py (see tests/core/
conftest.py's docstring): check_module/check_block are pure functions over
Block/Screen/Unit objects and are tested directly with hand-built objects
in test_autotest.py, no fixtures_app needed for those. But test()/
run_tests()/Recorder/the toolbar-button handlers genuinely need a real,
running User against real screens on disk -- test() reads JSON fixture
files from the `autotest` directory and drives them through
user.result4message()/user.prepare_result(), and Recorder captures real
traffic the same way server.py's websocket_handler does. This file follows
tests/core/conftest.py's app_client/new_user/wire_send/deliver pattern
closely for exactly that reason.

Layout expected by _app_on_path() below:
    <this dir>/fixtures_app/config.py
    <this dir>/fixtures_app/screens/*.py

Two bits of process-global state are specific to this directory (beyond
the User/Unishare/config globals tests/core already isolates):

  * unisi.autotest.recorder -- a module-level Recorder() singleton;
    record_file/record_buffer/ignored_1message are snapshotted/restored
    per test the same way tests/core isolates User.last_user etc.
  * the on-disk `autotest/` directory itself (testdir in utils.py) --
    test()/run_tests()/Recorder read and write real files there, relative
    to cwd (fixtures_app once _app_on_path has chdir'd into it). Cleared
    before and after every test so one test's recorded fixture can never
    leak into another's file-scanning (run_tests iterating testdir, or a
    filename collision).
"""
import os
import shutil
import sys
from pathlib import Path

import pytest
import pytest_asyncio

THIS_DIR = Path(__file__).parent
FIXTURES_APP = THIS_DIR / "fixtures_app"
AUTOTEST_DIR = FIXTURES_APP / "autotest"

UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))


def _clear_autotest_dir():
    if AUTOTEST_DIR.exists():
        shutil.rmtree(AUTOTEST_DIR)


@pytest.fixture(scope="session", autouse=True)
def _app_on_path():
    """Point sys.path/cwd at fixtures_app for the whole test session -- see
    tests/core/conftest.py's identical fixture for the full rationale."""
    from unisi.users import User

    old_cwd = os.getcwd()
    old_path = list(sys.path)
    os.chdir(FIXTURES_APP)
    sys.path.insert(0, str(FIXTURES_APP))

    User._screen_registry_ready = False
    User.screen_registry = []

    downloads_dir = FIXTURES_APP / "downloads"
    downloads_dir.mkdir(exist_ok=True)
    users_dir = FIXTURES_APP / "users"
    users_dir.mkdir(exist_ok=True)
    _clear_autotest_dir()

    yield

    os.chdir(old_cwd)
    sys.path[:] = old_path
    User._screen_registry_ready = False
    User.screen_registry = []
    _clear_autotest_dir()


@pytest.fixture(autouse=True)
def _isolate_autotest_state():
    """Snapshot/restore User/Unishare/config globals (same discipline as
    tests/core/conftest.py's _isolate_core_state) plus autotest.py's own
    recorder singleton and the on-disk autotest/ directory."""
    from unisi.users import User
    from unisi.common import Unishare
    from unisi.autotest import recorder
    import config

    last_user_snapshot = User.last_user
    count_snapshot = User.count
    sessions_snapshot = dict(Unishare.sessions)
    test_list_snapshot = list(Unishare.test_list)
    config_snapshot = dict(config.__dict__)
    recorder_file_snapshot = recorder.record_file
    recorder_buffer_snapshot = list(recorder.record_buffer)
    recorder_ignored_snapshot = getattr(recorder, "ignored_1message", None)

    yield

    User.last_user = last_user_snapshot
    User.count = count_snapshot
    Unishare.sessions.clear()
    Unishare.sessions.update(sessions_snapshot)
    Unishare.test_list[:] = test_list_snapshot
    config.__dict__.clear()
    config.__dict__.update(config_snapshot)
    recorder.record_file = recorder_file_snapshot
    recorder.record_buffer = recorder_buffer_snapshot
    recorder.ignored_1message = recorder_ignored_snapshot
    _clear_autotest_dir()


_session_counter = 0


@pytest.fixture
def new_user(_app_on_path):
    """Factory fixture: new_user(screen=None, session=None) -> a User.
    Mirrors tests/core/conftest.py's new_user exactly."""
    from unisi.users import User

    global _session_counter
    sessions_used = set()

    def _make(screen=None, session=None):
        global _session_counter
        if session is None:
            _session_counter += 1
            session = f"pytest_autotest_{os.getpid()}_{_session_counter}"
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
    coroutine function wired onto user.send. Mirrors tests/core/conftest.py's
    identical fixture."""

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
    server.py's websocket_handler loop. Mirrors tests/core/conftest.py's
    identical fixture."""
    from unisi.common import ReceivedMessage

    async def _deliver(user, block, element, event, value=None, persist=True):
        if not getattr(user, "send", None):
            wire_send(user)
        path = [element, *block.split('@')] if element else [block]
        msg = ReceivedMessage({"path": path, "event": event, "value": value})
        result = await user.result4message(msg)
        sent = await user.send(result, persist=persist)
        return result, sent

    return _deliver


@pytest_asyncio.fixture
async def app_client(_app_on_path):
    """A real aiohttp TestClient wired to server.py's actual /ws route --
    used specifically for the Recorder-vs-server.py integration regression
    test, where the bug lived in how websocket_handler wired send() and
    recorder.accept() together, not in either function alone. Mirrors
    tests/core/conftest.py's identical fixture."""
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
