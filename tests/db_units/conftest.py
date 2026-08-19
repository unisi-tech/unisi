"""
Shared pytest fixtures for the db.py / dbunits.py unit tests.

Unlike tests/users/ and tests/persist_voice_reloder/ (which need a real
fixtures_app on disk because User is architecturally tied to screen
loading), db.py's Database/Dbtable and dbunits.py's Dblist are pure
data-layer classes with no dependency on the screen/Unit machinery. Each
test gets its own ":memory:" SQLite database via the `db` fixture below --
sqlite3 accepts ":memory:" as a real, valid path, so no temp files, no
cross-test state, and no cleanup needed.
"""
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).parent
UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))


class RecordingLogger:
    """Stand-in for message_logger that records (type, message) pairs
    instead of printing.

    db.py deliberately swallows sqlite3.Error and most validation failures,
    reporting them through message_logger rather than raising -- tests for
    that behaviour need to assert *what was logged*, not just that nothing
    raised. Using a real callable (not unittest.mock.Mock) keeps call sites
    in db.py that do `if callable(Unishare.message_logger)`-style duck
    typing happy without needing to know about mocks.
    """

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def __call__(self, message: str, type: str = "error"):
        self.messages.append((type, message))

    @property
    def errors(self) -> list[str]:
        return [m for t, m in self.messages if t == "error"]

    @property
    def warnings(self) -> list[str]:
        return [m for t, m in self.messages if t == "warning"]

    def __bool__(self):
        # Some call sites may truthiness-check a configured logger; a
        # RecordingLogger should always count as "configured".
        return True


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def db(logger):
    """A fresh, isolated, in-memory Database for a single test."""
    from unisi.db import Database

    database = Database(":memory:", message_logger=logger)
    yield database
    database.close()


@pytest.fixture
def make_table(db):
    """Factory fixture: make_table(id='T', fields=..., limit=100, rows=None)
    -> Dbtable, using sensible defaults so most tests only need to override
    what they actually care about.
    """

    def _make(id="T", fields=None, limit=100, rows=None):
        if fields is None:
            fields = {"name": str, "age": int}
        return db.create_table(id, fields, limit=limit, rows=rows)

    return _make


@pytest.fixture
def table(make_table):
    """The common case: a single simple table, name/age, default limit."""
    return make_table()
