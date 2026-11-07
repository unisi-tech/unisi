"""
Fixtures for the agent API (unisi/mcp.py): a real User of fixtures_app (screens/agent.py declares `tools`) in
a real session, its views with a recording `send`, and the /mcp route on aiohttp's own test client -- the same
real-objects approach as tests/core/conftest.py, which this mirrors in brief.
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
    from unisi.users import User

    old_cwd, old_path = os.getcwd(), list(sys.path)
    os.chdir(FIXTURES_APP)
    sys.path.insert(0, str(FIXTURES_APP))
    User._screen_registry_ready = False         # class-level: another test dir may have filled it
    User.screen_registry = []
    for d in ("downloads", "users"):
        (FIXTURES_APP / d).mkdir(exist_ok=True)
    yield
    os.chdir(old_cwd)
    sys.path[:] = old_path
    User._screen_registry_ready = False
    User.screen_registry = []


@pytest.fixture(autouse=True)
def _isolate(_app_on_path):
    """Sessions, config and the users/*.db a test makes do not outlive it."""
    from unisi.users import User
    from unisi.common import Unishare
    import config

    sessions, last, cfg = dict(Unishare.sessions), User.last_user, dict(config.__dict__)
    before = set((FIXTURES_APP / "users").glob("*"))
    yield
    Unishare.sessions.clear()
    Unishare.sessions.update(sessions)
    User.last_user = last
    config.__dict__.clear()
    config.__dict__.update(cfg)
    for p in set((FIXTURES_APP / "users").glob("*")) - before:
        p.unlink(missing_ok=True)


_n = 0


@pytest.fixture
def session_id():
    global _n
    _n += 1
    return f"mcp_{os.getpid()}_{_n}"


def recording(user):
    """A recording `send` on a view, as server.websocket_handler wires the real one."""
    sent = []

    async def send(res, persist=True):
        if not isinstance(res, str):
            res = user.prepare_result(res, persist=persist)
        sent.append(res)
        return res

    user.send, user.ws = send, object()
    return sent


@pytest.fixture
def live(session_id):
    """A live session: its first user (the page of a person) and one more view of it, each with a recording send."""
    from unisi.sessions import open_session

    root = open_session(session_id)
    view = open_session(session_id, root, register=False)
    return session_id, root, recording(root), view, recording(view)


@pytest_asyncio.fixture
async def client(_app_on_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from unisi.mcp import handler
    from unisi.server import websocket_handler

    app = web.Application()
    app.add_routes([web.post('/mcp', handler), web.get('/mcp', handler), web.get('/ws', websocket_handler)])
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.fixture
def rpc(client):
    """rpc(session, method, params=None, id=1) -> the JSON answer of POST /mcp?session=..."""
    async def _rpc(session, method, params=None, id=1):
        body = {"jsonrpc": "2.0", "method": method, **({"id": id} if id is not None else {}),
                **({"params": params} if params is not None else {})}
        r = await client.post(f"/mcp?session={session}", json=body)
        return r.status, (await r.json() if r.status == 200 else None)
    return _rpc
