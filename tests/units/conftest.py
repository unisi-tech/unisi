"""
Shared pytest fixtures for the units.py / tables.py / graphs.py unit tests.

These three modules sit squarely in the "pure logic" camp described in
tests/db_units/conftest.py's docstring: Unit/ChangedProxy (units.py),
Table/PandaTable (tables.py) and Node/Edge/Graph/Net (graphs.py) don't
themselves do screen loading or message routing, so there's no need for a
real User backed by a fixtures_app on disk (contrast tests/users/ and
tests/persist_voice_reloder/, which genuinely need that).

They do, however, touch two bits of global framework state:

  * `Unishare.handle` (wired to the real `handle()` in server.py the moment
    `unisi` is imported) reads `User.last_user.handlers` -- used by
    Table.__init__ for persistent (id=...) tables, both for the
    unconditional `search` handler and, for linked tables, the `changed`/
    `filter` handlers.
  * `Unishare.db` -- persistent tables call `Unishare.db.set_db_list(self)`
    in __init__, and raise AssertionError if it's falsy.

Rather than construct a real User (which would pull in screen/session
machinery these modules don't otherwise touch), `fake_user` below is a
minimal stand-in exposing just what `handle()` and `set_reactivity()` need:
a `.handlers` dict and a `.register_changed_unit()` method. This keeps the
tests unit-scoped while still exercising the real `Unishare.handle`
decorator and the real `Unit.set_reactivity` reactivity machinery, not a
re-implementation of either.

Both `Unishare.handle`/`Unishare.db` and `User.last_user` are process-global,
so every fixture that touches them saves and restores the previous value --
the same discipline tests/persist_voice_reloder/conftest.py uses for
User.screen_registry, and for the same reason (these tests may run in the
same pytest session as tests/db_units, tests/users, tests/llmrag, ...).
"""
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).parent
UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))

from unisi.common import Unishare  # noqa: E402
from unisi.users import User  # noqa: E402
from unisi.db import Database  # noqa: E402


class FakeUser:
    """Minimal stand-in for User, covering exactly what these modules need:

      * `.handlers` -- a plain dict, so `Unishare.handle(unit, event)` (used
        by Table.__init__ for persistent/linked tables) has somewhere real
        to register into, and tests can assert on registrations directly
        (`(table, 'search') in fake_user.handlers`).
      * `.register_changed_unit(unit, property=None, value=None)` -- what
        `Unit.set_reactivity(user)` wires `unit._mark_changed` to call.
        Recorded in `.calls` so tests can assert *what* changed and *when*,
        the same way tests/persist_voice_reloder captures sent messages via
        wire_send() rather than mocking them away.

    Deliberately NOT a real User: no screens, no session, no persistence.
    Building one of those for a module that itself never touches screens or
    sessions would just be re-testing users.py under a different name.
    """

    def __init__(self):
        self.handlers: dict = {}
        self.calls: list[tuple] = []

    def register_changed_unit(self, unit, property=None, value=None):
        self.calls.append((unit, property, value))

    def changed_names(self):
        """Convenience: the `property` half of every recorded call, in
        order -- what most tests actually want to assert on."""
        return [p for _unit, p, _value in self.calls]


@pytest.fixture
def fake_user():
    """A fresh FakeUser, wired in as User.last_user for the duration of the
    test (restored after) so `Unishare.handle(...)` resolves to *this*
    fake's `.handlers` dict. Pass it directly to `unit.set_reactivity(...)`
    too -- in real usage `set_reactivity`'s `user` argument and
    `User.last_user` are the same object, so tests mirror that.
    """
    previous = User.last_user
    user = FakeUser()
    User.last_user = user
    yield user
    User.last_user = previous


@pytest.fixture
def memdb(fake_user):
    """A fresh, isolated, in-memory Database wired in as Unishare.db, for
    tests that construct persistent (id=...) Table instances. Depends on
    fake_user because every persistent Table also needs User.last_user for
    its unconditional `search` handler registration -- so any test using
    memdb gets a consistent fake_user for free instead of having to request
    both separately.

    ":memory:" gives every test its own private SQLite database (same
    reasoning as tests/db_units/conftest.py's `db` fixture) -- no temp
    files, no cross-test state.
    """
    previous_db = Unishare.db
    database = Database(":memory:", message_logger=print)
    Unishare.db = database
    yield database
    database.close()
    Unishare.db = previous_db


@pytest.fixture
def fake_get_property(monkeypatch):
    """Replaces the `get_property` name imported into both units.py and
    tables.py (each did `from .llmrag import get_property`, binding a
    separate local name in each module) with a recording async fake.

    This is a boundary mock, not a re-implementation: Unit.emit()/
    Table.emit() are only responsible for *deciding when to call*
    get_property and *what to do with the result* -- whether get_property
    itself correctly talks to an LLM is tests/llmrag's job, already covered
    there in detail.

    Usage:
        async def test_x(fake_get_property):
            fake_get_property.result = 42
            ...
            assert fake_get_property.calls[-1] == (name, context, type, options)
    """
    import unisi.units as units_mod
    import unisi.tables as tables_mod

    class FakeGetProperty:
        def __init__(self):
            self.calls: list[tuple] = []
            self.result = "llm-result"

        async def __call__(self, name, context="", type=str, options=None):
            self.calls.append((name, context, type, options))
            return self.result

    fake = FakeGetProperty()
    monkeypatch.setattr(units_mod, "get_property", fake)
    monkeypatch.setattr(tables_mod, "get_property", fake)
    return fake


@pytest.fixture
def llm_on(monkeypatch):
    """Sets Unishare.llm_model to a truthy sentinel for the duration of the
    test -- the outer gate both Unit.emit() and Table.emit() check before
    doing anything. monkeypatch reverts it automatically, including back to
    "unset" if it was never set before (monkeypatch.setattr's default
    raising=True would fail on a genuinely-missing attribute, but Unishare
    is an ArgObject whose __getattr__ returns None for unknown names rather
    than raising, so a plain setattr here is safe either way).
    """
    monkeypatch.setattr(Unishare, "llm_model", "test-llm-model")
    return "test-llm-model"


@pytest.fixture
def llm_off(monkeypatch):
    """The opposite of llm_on -- forces Unishare.llm_model to a falsy value.

    Needed because Unishare is process-global: tests/llmrag/conftest.py's
    own `fake_llm` fixture sets Unishare.llm_model without restoring it, so
    depending on test execution order within one pytest session,
    Unishare.llm_model could already be truthy by the time a test in *this*
    file runs. Tests asserting "emit() does nothing when the LLM is off"
    need this fixture rather than assuming ambient state.
    """
    monkeypatch.setattr(Unishare, "llm_model", None)
