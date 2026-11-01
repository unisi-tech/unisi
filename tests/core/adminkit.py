"""
Helpers of the tests of sessions.py and admin.py: sessions as files of the fixtures_app
working dir (`users/<id>.db`, `<upload_dir>/<id>`), and users "connected" without a socket.
"""
import os
import shutil
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import unisi.server as server_mod
from unisi.persist import Persist
from unisi.sessions import session_dir, session_registry

_n = 0


def sid(name):
    """a session id of its own for each test"""
    global _n
    _n += 1
    return f"adm-{name}-{os.getpid()}-{_n}"


def snapshot():
    found = set()
    for top in (Path("users"), Path("downloads")):
        if top.exists():
            found.add(top)
            found.update(top.rglob("*"))
    return found


def forget_new_files(before):
    for p in sorted(snapshot() - before, reverse=True):
        if p.is_symlink() or p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def save_state(session):
    """give the session a saved state, i.e. a footprint"""
    db = Persist(session)
    db.conn.execute("INSERT INTO state VALUES ('ns', 'p', '', '1', 0)")
    db.conn.commit()
    db.close()


def make_folder(session):
    folder = Path(session_dir(session))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "file.txt").write_text("x")
    return folder


def connect(session, **query):
    """a client connects, as websocket_handler does it"""
    qs = "&".join([f"session={session}", *(f"{k}={v}" for k, v in query.items())])
    user, _ = server_mod.make_user(make_mocked_request("GET", f"/ws?{qs}"))
    return user


class FakeWs:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def wire(user):
    """what websocket_handler gives a user: send() and ws"""
    user.ws = FakeWs()
    user.sent = []

    async def send(res, persist=True):
        user.sent.append(res)

    user.send = send
    return user


def ids(rows):
    return [r["id"] for r in rows]


def row(session):
    return next((r for r in session_registry.rows() if r["id"] == session), None)
