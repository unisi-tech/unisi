# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Shared pytest fixtures for the proxy.py unit tests.

proxy.py is UNISI's *client-side* helper: a small wrapper around a raw
websocket connection that mirrors, on the Python side, what a browser tab
does with a screen's JSON -- build outgoing request dicts, decode incoming
ones, keep a local copy of the current screen in sync. Its correctness is
entirely about message plumbing, not about any of the reactive/persistence
machinery covered by tests/units, tests/db_units, etc. So unlike tests/users
(which spins up a real User against a real fixtures_app) or tests/units
(which needs fake_user for Unit reactivity), these tests never need a real
server, a real User, or Unishare state -- just a scripted transport.

The one thing that has to be faked is that transport: Proxy.__init__ calls
websocket.create_connection(...) unconditionally and immediately performs a
blocking recv() to fetch the initial screen. FakeConnection below stands in
for the object create_connection() would normally return -- a scripted
send/recv double, not a mock of Proxy's own logic -- so every test still
exercises the real Proxy.__init__ / request() / process() / update() code,
just talking to a fake wire instead of a real one. This mirrors how
tests/persist_voice_reloder's conftest wires a fake `send` onto a *real*
User instead of re-implementing User's own sending logic.

Fixture/screen data itself is never hand-typed as raw dicts. Building the
nested {'name':..., 'type':..., 'value': [...]} shape by hand risks silently
encoding a *wrong* guess about the wire format and then "confirming" that
same wrong guess in the assertions. Instead, screen_dict()/block_dict()
below build real unisi.units.Unit / unisi.containers.Block instances and
round-trip them through the real toJson()/json.loads(), so every fixture is
byte-for-byte what a real server would actually send. real_update_message()
goes a step further for update() tests specifically: it drives the actual
unisi.common.Message / unisi.users.User.find_path pipeline (the exact code
users.py itself uses to build an outgoing 'update' message, path field
included) against real Block/Unit objects, rather than hand-guessing what a
path list should look like for a given nesting depth.
"""
import json
import sys
import types
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).parent
UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))

import unisi.proxy as proxy_module  # noqa: E402
from unisi.proxy import Proxy  # noqa: E402
from unisi.common import ArgObject, Message, toJson  # noqa: E402
from unisi.containers import Block  # noqa: E402
from unisi.users import User  # noqa: E402


# ──────────────────────────────────────────────────────────────────────── #
#  Fake transport                                                          #
# ──────────────────────────────────────────────────────────────────────── #

class FakeConnection:
    """Stand-in for the object websocket.create_connection(...) returns.

    Records every outgoing send() (raw JSON strings, exactly as
    Proxy.request would transmit them) in `.sent`, and serves pre-programmed
    payloads from an internal queue on each recv() call, in the order they
    were queued.
    """

    def __init__(self):
        self.sent: list[str] = []
        self._queue: list[str] = []
        self.closed = False
        # populated by the fake create_connection() in fake_conn below
        self.address = None
        self.timeout = None
        self.header = None

    def queue_raw(self, raw: str):
        """Queue an already-encoded string, returned verbatim by the next recv()."""
        self._queue.append(raw)

    def queue_message(self, message: dict):
        """Queue a dict, JSON-encoded exactly like a real server payload."""
        self._queue.append(json.dumps(message))

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        if not self._queue:
            raise AssertionError(
                'FakeConnection.recv() called with nothing queued -- the '
                'test forgot to queue_message()/queue_raw() a response '
                'before the code under test tried to receive one (or the '
                'code under test performed one more send/recv round-trip '
                'than the test expected).'
            )
        return self._queue.pop(0)

    def close(self):
        self.closed = True

    @property
    def last_sent(self) -> dict:
        """The most recent outgoing payload, decoded back to a dict, for
        assertions -- tests should never need to hand-parse JSON strings."""
        assert self.sent, 'nothing was sent on this connection yet'
        return json.loads(self.sent[-1])


@pytest.fixture
def fake_conn(monkeypatch):
    """Patches the `create_connection` name proxy.py imported into its own
    module namespace (`from websocket import create_connection`), so
    constructing a Proxy never touches a real socket. Returns the
    FakeConnection instance every Proxy() built via make_proxy() in this
    test will be wired to.

    Capturing the constructor's own arguments (address/timeout/header) onto
    the connection lets tests that care about the connection URL (e.g. the
    session/screen query-string tests) inspect conn.address afterwards,
    without needing a second monkeypatch just for that.
    """
    conn = FakeConnection()

    def fake_create_connection(address, timeout=7, **kwargs):
        conn.address = address
        conn.timeout = timeout
        conn.header = kwargs.get('header')
        return conn

    monkeypatch.setattr(proxy_module, 'create_connection', fake_create_connection)
    return conn


def make_proxy(fake_conn, initial_message=None, **proxy_kwargs) -> Proxy:
    """Queue *initial_message* (defaulting to a minimal screen with no
    blocks) as the first thing the new Proxy's __init__ will recv(), then
    construct and return a real Proxy wired to fake_conn.

    host_port defaults to 'localhost:8000' unless passed explicitly in
    proxy_kwargs (e.g. to exercise the ssl=True / non-localhost paths).
    """
    if initial_message is None:
        initial_message = screen_dict()
    fake_conn.queue_message(initial_message)
    proxy_kwargs.setdefault('host_port', 'localhost:8000')
    host_port = proxy_kwargs.pop('host_port')
    return Proxy(host_port, **proxy_kwargs)


# ──────────────────────────────────────────────────────────────────────── #
#  Authentic screen/block/element fixtures                                 #
# ──────────────────────────────────────────────────────────────────────── #

def wire(obj):
    """Serialise any single real Unit/Block/Dialog/Message instance (or
    plain dict/list) through the real toJson(), then decode back to a plain
    dict/list -- exactly the shape that arrives over the wire, never
    hand-typed. Use this (not to_wire) when the object itself IS the
    message, e.g. a Dialog -- to_wire always wraps its arguments in a list,
    which is right for a block's/screen's 'value'/'blocks' list but wrong
    for a single top-level message."""
    return json.loads(toJson(obj))


def to_wire(*units):
    """Serialise real Unit/Block instances through the real toJson(), then
    decode back to plain dicts/lists -- exactly the shape that arrives over
    the wire, never hand-typed. Always returns a *list* (even for one
    unit), matching a block's/screen's own 'value'/'blocks' shape -- see
    wire() for the single-object case."""
    return wire(list(units))


def screen_dict(name='Home', blocks=(), toolbar=(), menu=None, **extra) -> dict:
    """Build a 'screen'-type message dict. *blocks*/*toolbar* may be real
    Block/Unit instances (serialised automatically) or already-plain dicts
    (passed through as-is, so tests can also compose screens out of the
    output of an earlier to_wire() call without double-encoding).

    menu defaults to a single [name, 'home'] entry, matching
    User.update_menu()'s [[info.name, info.icon], ...] shape.
    """
    def _wire(items):
        items = list(items)
        if items and isinstance(items[0], dict):
            return items
        return to_wire(*items)

    return {
        'type': 'screen',
        'name': name,
        'blocks': _wire(blocks),
        'toolbar': _wire(toolbar),
        'menu': menu if menu is not None else [[name, 'home']],
        **extra,
    }


class FakeServerUser:
    """Just enough of a User for the *real* unisi.users.User.find_path to
    run against: a plain `.blocks` list and a `.screen.toolbar` list.
    find_path only ever reads those two things, so binding the real
    (unbound) method onto this tiny stand-in drives the exact path-building
    logic users.py itself uses -- not a hand-guessed re-implementation of
    it -- without needing a full User (session, screens dir, DB, ...).
    """

    def __init__(self, blocks=(), toolbar=()):
        self.blocks = list(blocks)
        self.screen = ArgObject(toolbar=list(toolbar))
        self.find_path = types.MethodType(User.find_path, self)


def real_update_message(*units, blocks=(), toolbar=(), mtype='update') -> dict:
    """Build a genuine wire-format update message for *units by running them
    through the real Message(...)/fill_paths4()/find_path() pipeline --
    the same code users.py itself uses to build what actually goes out over
    the wire -- against a tree of real Block/Unit objects, then round-trips
    it through JSON exactly like the wire does.

    blocks/toolbar describe the root-level tree *units live somewhere
    inside (they don't need to be the units themselves, just contain them,
    the way a real screen's blocks/toolbar would).
    """
    fake_user = FakeServerUser(blocks, toolbar)
    message = Message(*units, user=fake_user, type=mtype)
    return json.loads(toJson(message))


# ──────────────────────────────────────────────────────────────────────── #
#  Small ready-made element/block factories (thin wrappers over the real
#  Unit subclasses, for tests that don't care about a specific widget type)
# ──────────────────────────────────────────────────────────────────────── #

@pytest.fixture
def make_block():
    """Factory fixture: make_block(name, *elems, **options) -> a real
    unisi.containers.Block instance (not yet serialised) -- for tests that
    need to keep the live Python object around (e.g. to mutate it and build
    a real_update_message from it) as well as its wire form.
    """
    return Block
