# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/db.py -- the Database and Dbtable classes, plus the
module-level type system (normalize_field_types, sqlite_data_type,
_adapt_value / _convert_value) and Smart Schema Evolution.

Organisation
────────────
  TestNormalizeFieldTypes   - field-spec normalisation (types/strings/mixed)
  TestSqliteDataType        - Python value -> declared SQLite type inference
  TestDatabaseBasics        - execute/qlist/qiter/table_names/get_table_fields
  TestCreateTable           - Database.create_table
  TestGetTable              - Database.get_table (incl. type inference from rows)
  TestSchemaMigration       - Smart Schema Evolution (interactive_migration_choice)
  TestTypeRoundTrip         - every supported type survives a write/read cycle
  TestRowCRUD               - append_row/append_rows/delete_row/delete_rows/clear
  TestSearch                - search_rows / _build_search_where
  TestManyToOne             - setup_fk/set_fk/clear_fk/calc_linked_rows_fk
  TestManyToMany            - setup_junction/add_link/delete_link(s)/calc_linked_rows
  TestVersionCounter        - Dbtable._version bump semantics (see dbunits.py)

Regression tests for bugs found while writing this suite are marked
"Regression:" in their docstring, with a short description of the bug.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from unisi.db import (
    Database,
    Dbtable,
    _adapt_value,
    _convert_value,
    _equal_field_dicts,
    normalize_field_types,
    sqlite_data_type,
)


# ────────────────────────────────────────────────────────────────────────── #
#  Type system                                                                #
# ────────────────────────────────────────────────────────────────────────── #

class TestNormalizeFieldTypes:
    def test_python_types(self):
        assert normalize_field_types({"name": str, "age": int}) == {
            "name": "TEXT", "age": "INTEGER",
        }

    def test_sqlite_strings_are_uppercased(self):
        assert normalize_field_types({"name": "text", "age": "Integer"}) == {
            "name": "TEXT", "age": "INTEGER",
        }

    def test_mixed_types_and_strings(self):
        assert normalize_field_types({"age": int, "note": "TEXT"}) == {
            "age": "INTEGER", "note": "TEXT",
        }

    def test_all_documented_python_types(self):
        fields = {
            "a": bool, "b": int, "c": float, "d": str, "e": bytes,
            "f": list, "g": tuple, "h": dict, "i": datetime, "j": date,
            "k": Decimal, "l": uuid.UUID,
        }
        result = normalize_field_types(fields)
        assert result == {
            "a": "BOOLEAN", "b": "INTEGER", "c": "REAL", "d": "TEXT",
            "e": "BLOB", "f": "JSON", "g": "JSON", "h": "JSON",
            "i": "TIMESTAMP", "j": "DATE", "k": "DECIMAL", "l": "UUID",
        }

    def test_unsupported_python_type_raises(self):
        class Widget:
            pass

        with pytest.raises(TypeError):
            normalize_field_types({"w": Widget})

    def test_non_type_non_str_spec_raises(self):
        with pytest.raises(TypeError):
            normalize_field_types({"age": 42})

    def test_is_idempotent(self):
        """create_table() relies on this: it normalises fields that may
        already have been normalised once by get_table()."""
        once = normalize_field_types({"name": str, "age": "integer"})
        twice = normalize_field_types(once)
        assert once == twice == {"name": "TEXT", "age": "INTEGER"}


class TestSqliteDataType:
    @pytest.mark.parametrize("value, expected", [
        (True, "BOOLEAN"),
        (False, "BOOLEAN"),
        (1, "INTEGER"),
        (-5, "INTEGER"),
        (3.14, "REAL"),
        ("hello", "TEXT"),
        (b"bytes", "BLOB"),
        ([1, 2], "JSON"),
        ((1, 2), "JSON"),
        ({"a": 1}, "JSON"),
        (Decimal("1.5"), "DECIMAL"),
    ])
    def test_value_to_type(self, value, expected):
        assert sqlite_data_type(value) == expected

    def test_bool_before_int_in_match_order(self):
        """bool is a subclass of int in Python; sqlite_data_type must match
        bool *before* int or every boolean would be misreported as INTEGER."""
        assert sqlite_data_type(True) == "BOOLEAN"
        assert sqlite_data_type(1) == "INTEGER"

    def test_datetime_before_date_in_match_order(self):
        """datetime is a subclass of date; a datetime instance must match
        the datetime case before falling through to the date case."""
        assert sqlite_data_type(datetime(2024, 1, 1, 12, 0, 0)) == "TIMESTAMP"
        assert sqlite_data_type(date(2024, 1, 1)) == "DATE"

    def test_uuid(self):
        assert sqlite_data_type(uuid.uuid4()) == "UUID"

    def test_unsupported_value_returns_empty_string(self):
        class Widget:
            pass

        assert sqlite_data_type(Widget()) == ""
        assert sqlite_data_type(None) == ""


class TestAdaptConvertHelpers:
    """Direct tests of the _adapt_value/_convert_value pair (the full
    round-trip through an actual DB column is covered by
    TestTypeRoundTrip below; these test the pure functions in isolation)."""

    def test_adapt_none_passthrough(self):
        assert _adapt_value(None) is None

    def test_adapt_bool_to_int(self):
        assert _adapt_value(True) == 1
        assert _adapt_value(False) == 0

    def test_adapt_json_types(self):
        assert _adapt_value([1, 2]) == "[1, 2]"
        assert _adapt_value({"a": 1}) == '{"a": 1}'

    def test_adapt_decimal_and_uuid_to_str(self):
        d = Decimal("1.23")
        u = uuid.uuid4()
        assert _adapt_value(d) == str(d)
        assert _adapt_value(u) == str(u)

    def test_adapt_datetime_and_date_to_isoformat(self):
        dt = datetime(2024, 1, 1, 12, 30)
        d = date(2024, 1, 1)
        assert _adapt_value(dt) == dt.isoformat()
        assert _adapt_value(d) == d.isoformat()

    def test_adapt_plain_values_passthrough(self):
        assert _adapt_value(42) == 42
        assert _adapt_value(3.14) == 3.14
        assert _adapt_value("x") == "x"

    def test_convert_none_passthrough(self):
        assert _convert_value(None, "INTEGER") is None

    def test_convert_boolean(self):
        assert _convert_value(1, "BOOLEAN") is True
        assert _convert_value(0, "BOOLEAN") is False
        assert _convert_value(True, "BOOLEAN") is True  # already-converted

    def test_convert_json(self):
        assert _convert_value("[1, 2]", "JSON") == [1, 2]
        assert _convert_value([1, 2], "JSON") == [1, 2]  # already-converted

    def test_convert_decimal_uuid_date_timestamp(self):
        assert _convert_value("1.23", "DECIMAL") == Decimal("1.23")
        u = uuid.uuid4()
        assert _convert_value(str(u), "UUID") == u
        assert _convert_value("2024-01-01", "DATE") == date(2024, 1, 1)
        assert _convert_value("2024-01-01T12:30:00", "TIMESTAMP") == datetime(2024, 1, 1, 12, 30)

    def test_convert_corrupt_value_falls_back_to_raw(self):
        """A value that can't be parsed as the declared type must not raise
        -- it should come back unchanged so one bad cell can't break a
        whole SELECT."""
        assert _convert_value("not-a-uuid", "UUID") == "not-a-uuid"
        assert _convert_value("not-json{", "JSON") == "not-json{"


class TestEqualFieldDicts:
    def test_equal_ignoring_case(self):
        assert _equal_field_dicts({"a": "text"}, {"a": "TEXT"})

    def test_different_keys(self):
        assert not _equal_field_dicts({"a": "TEXT"}, {"b": "TEXT"})

    def test_different_types(self):
        assert not _equal_field_dicts({"a": "TEXT"}, {"a": "INTEGER"})


# ────────────────────────────────────────────────────────────────────────── #
#  Database basics                                                            #
# ────────────────────────────────────────────────────────────────────────── #

class TestDatabaseBasics:
    def test_execute_success_returns_cursor_and_commits(self, db):
        db.execute("CREATE TABLE [X] (v TEXT, ID INTEGER PRIMARY KEY AUTOINCREMENT)")
        cur = db.execute("INSERT INTO [X] (v) VALUES (?)", ("hi",))
        assert cur is not None
        # A second, independent cursor on the same connection sees the row,
        # proving execute() actually committed rather than leaving it
        # pending in an uncommitted transaction.
        rows = db._conn.execute("SELECT v FROM [X]").fetchall()
        assert [r[0] for r in rows] == ["hi"]

    def test_execute_failure_logs_and_returns_none(self, db, logger):
        result = db.execute("NOT VALID SQL")
        assert result is None
        assert len(logger.errors) == 1
        assert "SQL Error" in logger.errors[0]

    def test_execute_ignore_exception_suppresses_logging(self, db, logger):
        result = db.execute("NOT VALID SQL", ignore_exception=True)
        assert result is None
        assert logger.messages == []

    def test_executemany(self, db):
        db.execute("CREATE TABLE [X] (v INTEGER, ID INTEGER PRIMARY KEY AUTOINCREMENT)")
        cur = db.executemany("INSERT INTO [X] (v) VALUES (?)", [(1,), (2,), (3,)])
        assert cur is not None
        rows = db._conn.execute("SELECT v FROM [X] ORDER BY v").fetchall()
        assert [r[0] for r in rows] == [1, 2, 3]

    def test_executemany_failure_logs_and_returns_none(self, db, logger):
        assert db.executemany("NOT VALID SQL", [(1,)]) is None
        assert "executemany" in logger.errors[0]

    def test_table_names(self, db, make_table):
        assert db.table_names == []
        make_table(id="Alpha")
        make_table(id="Beta")
        assert db.table_names == ["Alpha", "Beta"]

    def test_get_table_fields_removes_id_by_default(self, db, table):
        fields = db.get_table_fields("T")
        assert fields == {"name": "TEXT", "age": "INTEGER"}

    def test_get_table_fields_can_keep_id(self, db, table):
        fields = db.get_table_fields("T", remove_id=False)
        assert fields == {"name": "TEXT", "age": "INTEGER", "ID": "INTEGER"}

    def test_get_table_fields_nonexistent_table_returns_none(self, db):
        assert db.get_table_fields("Ghost") is None

    def test_delete_table(self, db, table):
        assert "T" in db.table_names
        assert db.delete_table("T") is True
        assert "T" not in db.table_names

    def test_delete_table_nonexistent_is_a_harmless_no_op(self, db):
        # DROP TABLE IF EXISTS never errors, so this returns True even
        # though nothing was there to delete.
        assert db.delete_table("Ghost") is True

    def test_creates_parent_directory_for_file_based_db(self, tmp_path, logger):
        nested = tmp_path / "sub" / "dir" / "app.db"
        assert not nested.parent.exists()
        database = Database(str(nested), message_logger=logger)
        try:
            assert nested.parent.exists()
        finally:
            database.close()

    def test_qlist_returns_rows_as_plain_lists(self, db, table):
        table.append_row(["Alice", 30])
        table.append_row(["Bob", 25])
        result = db.qlist("SELECT name, age FROM [T] ORDER BY name")
        assert result == [["Alice", 30], ["Bob", 25]]

    def test_qlist_applies_func(self, db, table):
        table.append_row(["Alice", 30])
        result = db.qlist("SELECT name FROM [T]", func=lambda r: r[0].upper())
        assert result == ["ALICE"]

    def test_qlist_returns_none_on_error(self, db, logger):
        assert db.qlist("NOT VALID SQL") is None

    def test_qiter_yields_rows(self, db, table):
        table.append_row(["Alice", 30])
        table.append_row(["Bob", 25])
        assert list(db.qiter("SELECT name FROM [T] ORDER BY name")) == [["Alice"], ["Bob"]]

    def test_qiter_yields_nothing_on_error(self, db):
        assert list(db.qiter("NOT VALID SQL")) == []

    def test_update_row(self, db, table):
        row = table.append_row(["Alice", 30])
        row_id = row[-1]
        assert db.update_row("T", row_id, {"age": 31}) is True
        assert table.read_rows()[0] == ["Alice", 31, row_id]


# ────────────────────────────────────────────────────────────────────────── #
#  create_table                                                               #
# ────────────────────────────────────────────────────────────────────────── #

class TestCreateTable:
    def test_creates_schema_and_returns_dbtable(self, db):
        t = db.create_table("T", {"name": "TEXT", "age": "INTEGER"})
        assert isinstance(t, Dbtable)
        assert t.node_columns == ["name", "age"]
        assert "T" in db.table_names

    def test_with_rows_populates_table(self, db):
        t = db.create_table(
            "T", {"name": "TEXT"}, rows=[["Alice"], ["Bob"]]
        )
        assert len(t.list) == 2
        assert [r[0] for r in t.list] == ["Alice", "Bob"]

    def test_if_not_exists_is_safe_to_call_twice(self, db):
        db.create_table("T", {"name": "TEXT"}, rows=[["Alice"]])
        db.create_table("T", {"name": "TEXT"})  # must not raise / drop data
        assert db.qlist("SELECT name FROM [T]") == [["Alice"]]

    def test_regression_normalizes_python_types(self, db):
        """
        Regression: create_table() used to interpolate the raw `fields`
        values straight into the CREATE TABLE statement without
        normalising, unlike get_table()/setup_junction() which both call
        normalize_field_types() first. Calling create_table() directly
        with Python types (as the module docstring's type table implies is
        supported everywhere) produced a SQL syntax error such as
        "[name] <class 'str'>" instead of "[name] TEXT".
        """
        t = db.create_table("T", {"name": str, "age": int})
        assert t.table_fields == {"name": "TEXT", "age": "INTEGER"}
        row = t.append_row(["Alice", 30])
        assert row == ["Alice", 30, 1]


# ────────────────────────────────────────────────────────────────────────── #
#  get_table                                                                  #
# ────────────────────────────────────────────────────────────────────────── #

class TestGetTable:
    def test_returns_none_for_falsy_id(self, db):
        assert db.get_table(id=None) is None
        assert db.get_table(id="") is None

    def test_creates_new_table_when_absent(self, db):
        t = db.get_table("T", fields={"name": str})
        assert isinstance(t, Dbtable)
        assert "T" in db.table_names

    def test_returns_same_instance_on_repeat_call(self, db):
        t1 = db.get_table("T", fields={"name": str})
        t2 = db.get_table("T", fields={"name": str})
        assert t1 is t2

    def test_wraps_pre_existing_table_without_fields_arg(self, db):
        db.execute("CREATE TABLE [T] (name TEXT, ID INTEGER PRIMARY KEY AUTOINCREMENT)")
        t = db.get_table("T")
        assert t.node_columns == ["name"]

    def test_infers_types_from_rows_and_headers(self, db):
        t = db.get_table(
            "T", headers=["name", "age"], rows=[["Alice", 30], ["Bob", 25]]
        )
        assert t.table_fields == {"name": "TEXT", "age": "INTEGER"}
        assert len(t.list) == 2

    def test_infers_real_from_mixed_int_and_float_column(self, db):
        t = db.get_table(
            "T", headers=["name", "score"], rows=[["Alice", 1], ["Bob", 2.5]]
        )
        assert t.table_fields["score"] == "REAL"

    def test_infers_type_skipping_leading_none_cells(self, db):
        """A None in the first row for a column shouldn't stop later rows
        from being used to infer that column's type."""
        t = db.get_table(
            "T", headers=["name", "age"], rows=[["Alice", None], ["Bob", 25]]
        )
        assert t.table_fields["age"] == "INTEGER"

    def test_conflicting_incompatible_types_returns_none_and_warns(self, db, logger):
        result = db.get_table(
            "T", headers=["name", "mixed"], rows=[["Alice", "text"], ["Bob", 5]]
        )
        assert result is None
        assert len(logger.warnings) == 1

    def test_rows_without_headers_returns_none_and_logs(self, db, logger):
        result = db.get_table("T", rows=[["Alice", 30]])
        assert result is None
        assert any("headers" in m for m in logger.errors)

    def test_all_none_column_cannot_infer_type_returns_none(self, db, logger):
        result = db.get_table(
            "T", headers=["name", "mystery"], rows=[["Alice", None], ["Bob", None]]
        )
        assert result is None
        assert any("Cannot infer type" in m for m in logger.errors)

    def test_existing_table_matching_fields_returns_wrapper(self, db):
        db.get_table("T", fields={"name": str})
        t2 = db.get_table("T", fields={"name": "text"})  # same type, different case/form
        assert t2.node_columns == ["name"]

    def test_get_table_params_filters_to_known_kwargs(self, db):
        params = db.get_table_params(
            {"id": "T", "limit": 50, "fields": {"name": str}, "unrelated": 123}
        )
        assert params == {"id": "T", "limit": 50, "fields": {"name": str}}
        assert "unrelated" not in params


# ────────────────────────────────────────────────────────────────────────── #
#  Smart Schema Evolution                                                     #
# ────────────────────────────────────────────────────────────────────────── #

class TestSchemaMigration:
    """
    get_table() detects a field mismatch against an existing table and
    delegates to _migrate_table(), which calls interactive_migration_choice()
    -- a console prompt via input(). Every test here monkeypatches
    builtins.input so no test actually blocks on stdin.
    """

    def _seed(self, db):
        t = db.get_table("T", fields={"name": str, "age": int})
        t.append_row(["Alice", 30])
        t.append_row(["Bob", 25])
        return t

    def test_cancel_keeps_old_table_and_data(self, db, monkeypatch):
        self._seed(db)
        monkeypatch.setattr("builtins.input", lambda prompt="": "1")

        result = db.get_table("T", fields={"name": str, "age": int, "extra": str})

        assert result is not None
        assert result.node_columns == ["name", "age"]  # old schema, unchanged
        assert db.qlist("SELECT name, age FROM [T] ORDER BY name") == [
            ["Alice", 30], ["Bob", 25],
        ]

    def test_unrecognized_choice_also_cancels(self, db, monkeypatch):
        self._seed(db)
        monkeypatch.setattr("builtins.input", lambda prompt="": "banana")
        result = db.get_table("T", fields={"name": str, "age": int, "extra": str})
        assert result.node_columns == ["name", "age"]

    def test_eof_from_input_cancels(self, db, monkeypatch):
        self._seed(db)

        def raise_eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        result = db.get_table("T", fields={"name": str, "age": int, "extra": str})
        assert result.node_columns == ["name", "age"]

    def test_recreate_drops_old_data(self, db, monkeypatch):
        self._seed(db)
        monkeypatch.setattr("builtins.input", lambda prompt="": "2")

        result = db.get_table("T", fields={"name": str, "score": float})

        assert result.node_columns == ["name", "score"]
        assert db.qlist("SELECT * FROM [T]") == []  # old rows gone

    def test_exact_match_migration_preserves_matched_columns(self, db, monkeypatch):
        self._seed(db)
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")

        # 'note' is brand new (no match); 'name'/'age' match exactly.
        result = db.get_table(
            "T", fields={"name": str, "age": int, "note": str}
        )

        assert result.node_columns == ["name", "age", "note"]
        rows = db.qlist("SELECT name, age, note FROM [T] ORDER BY name")
        assert rows == [["Alice", 30, None], ["Bob", 25, None]]

    def test_exact_match_migration_keeps_backup_table(self, db, monkeypatch):
        self._seed(db)
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")
        db.get_table("T", fields={"name": str, "age": int, "note": str})

        backups = [n for n in db.table_names if n.startswith("T_OLD_")]
        assert len(backups) == 1
        assert db.qlist(f"SELECT name, age FROM [{backups[0]}] ORDER BY name") == [
            ["Alice", 30], ["Bob", 25],
        ]

    def test_exact_match_migration_preserves_row_ids(self, db, monkeypatch):
        t = self._seed(db)
        alice_id = t.list[0][-1]
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")

        result = db.get_table("T", fields={"name": str, "age": int, "note": str})

        alice_row = next(r for r in result.list if r[0] == "Alice")
        assert alice_row[-1] == alice_id

    def test_fuzzy_match_migration_maps_renamed_column(self, db, monkeypatch):
        # 'nmae' is a one-edit-away typo for 'name' -- close enough for
        # difflib's default cutoff (0.6) to treat it as a fuzzy match.
        t = db.get_table("T", fields={"nmae": str})
        t.append_row(["Alice"])
        monkeypatch.setattr("builtins.input", lambda prompt="": "4")

        result = db.get_table("T", fields={"name": str})

        assert result.node_columns == ["name"]
        assert db.qlist("SELECT name FROM [T]") == [["Alice"]]

    def test_second_migration_gets_its_own_backup_name(self, db, monkeypatch):
        """_find_backup_name must not collide with a backup left by an
        earlier migration of the same table."""
        self._seed(db)
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")
        db.get_table("T", fields={"name": str, "age": int, "note": str})
        db.get_table("T", fields={"name": str, "age": int, "note2": str})

        backups = sorted(n for n in db.table_names if n.startswith("T_OLD_"))
        assert backups == ["T_OLD_1", "T_OLD_2"]

    def test_migration_not_triggered_when_fields_match(self, db, monkeypatch):
        """No prompt at all (input would raise if called) when the
        requested schema is identical to what's already there."""
        self._seed(db)

        def boom(prompt=""):
            raise AssertionError("input() should not be called")

        monkeypatch.setattr("builtins.input", boom)
        result = db.get_table("T", fields={"name": str, "age": int})
        assert result.node_columns == ["name", "age"]


# ────────────────────────────────────────────────────────────────────────── #
#  Type round-trip through a real column                                      #
# ────────────────────────────────────────────────────────────────────────── #

class TestTypeRoundTrip:
    """Every documented type must survive INSERT -> SELECT with both its
    Python type and its value unchanged."""

    @pytest.mark.parametrize("field_type, value", [
        (bool, True),
        (bool, False),
        (int, 42),
        (int, -17),
        (float, 3.14),
        (str, "hello world"),
        (str, ""),
        (bytes, b"\x00\x01binary"),
        (datetime, datetime(2024, 6, 15, 9, 30, 0)),
        (date, date(2024, 6, 15)),
        (list, [1, "two", 3.0]),
        (dict, {"nested": {"a": 1}}),
        (Decimal, Decimal("12345.6789")),
        (uuid.UUID, uuid.uuid4()),
    ])
    def test_round_trip(self, db, field_type, value):
        t = db.create_table("T", {"v": field_type})
        t.append_row([value])
        got = t.read_rows()[0][0]
        assert got == value
        assert type(got) is type(value)

    def test_none_round_trips_for_every_type(self, db):
        t = db.create_table(
            "T", {"a": int, "b": str, "c": datetime, "d": Decimal, "e": uuid.UUID}
        )
        # dict-form so at least one column is explicitly present; an
        # all-None *list* row is covered on its own below, since it hits a
        # different code path (see test_regression_all_none_list_row...).
        t.append_row({"a": None, "b": "x", "c": None, "d": None, "e": None})
        row = t.read_rows()[0]
        assert row[:5] == [None, "x", None, None, None]

    def test_regression_all_none_list_row_does_not_produce_invalid_sql(self, db):
        """
        Regression: a list-row where every value is None (or an empty
        dict-row) filters down to an empty `props`/`d` in append_row() /
        append_rows(), which used to build "INSERT INTO t () VALUES ()" --
        invalid SQLite syntax ("near ')': syntax error"). This is exactly
        what tables.py's append_table_row() sends for the standard "add a
        blank row" UI button (`[None] * len(headers)`), so the bug broke
        that button outright for any table reached that way.
        """
        t = db.create_table("T", {"name": "TEXT", "age": "INTEGER"})

        row = t.append_row([None, None])
        assert row == [None, None, 1]
        assert t.length == 1

        # And the row must be a real, editable DB row afterwards -- the
        # whole point of inserting a blank row is to fill it in next.
        assert t.assign_row(["Alice", 30, row[-1]]) is True
        assert t.read_rows() == [["Alice", 30, 1]]

    def test_regression_all_empty_dict_batch_does_not_produce_invalid_sql(self, db):
        """Same bug, append_rows() bulk-insert form: a batch of entirely
        empty dict-rows must not crash."""
        t = db.create_table("T", {"name": "TEXT", "age": "INTEGER"})
        inserted = t.append_rows([{}, {}])
        assert inserted == [[None, None, 1], [None, None, 2]]
        assert t.length == 2

    def test_empty_list_and_dict_round_trip(self, db):
        t = db.create_table("T", {"a": list, "b": dict})
        t.append_row([[], {}])
        row = t.read_rows()[0]
        assert row[0] == []
        assert row[1] == {}


# ────────────────────────────────────────────────────────────────────────── #
#  Row CRUD                                                                   #
# ────────────────────────────────────────────────────────────────────────── #

class TestRowCRUD:
    def test_append_row_list_form(self, db, table):
        assert table.append_row(["Alice", 30]) == ["Alice", 30, 1]

    def test_append_row_dict_form(self, db, table):
        assert table.append_row({"name": "Alice", "age": 30}) == ["Alice", 30, 1]

    def test_append_row_list_form_filters_none(self, db):
        """A None in a list-row means 'leave this column at its SQL
        default (NULL)', matching the dict-row behaviour below -- not an
        explicit overwrite attempt."""
        t = db.create_table("T", {"name": "TEXT", "age": "INTEGER"})
        row = t.append_row(["Alice", None])
        assert row == ["Alice", None, 1]

    def test_append_row_dict_form_filters_none(self, db, table):
        row = table.append_row({"name": "Alice", "age": None})
        assert row == ["Alice", None, 1]

    def test_append_row_extra_list_values_are_truncated(self, db, table):
        """A row longer than node_columns (e.g. a full row re-inserted
        with its old trailing ID) only uses the first len(node_columns)
        values; the rest are ignored rather than erroring."""
        row = table.append_row(["Alice", 30, 999])
        assert row == ["Alice", 30, 1]

    def test_append_row_rejects_non_list_non_dict(self, db, table):
        with pytest.raises(TypeError):
            table.append_row("not a row")

    def test_append_row_returns_none_and_logs_on_sql_failure(self, db, table, logger):
        result = table.append_row({"no_such_column": "x"})
        assert result is None
        assert len(logger.errors) == 1

    def test_append_row_failure_does_not_change_length(self, db, table):
        table.append_row(["Alice", 30])
        table.append_row({"no_such_column": "x"})
        assert table.length == 1

    def test_regression_append_rows_preserves_columns_missing_from_first_dict(self, db):
        """
        Regression: append_rows() used to build its INSERT's column list
        from dicts[0].keys() only. A batch where the first row happened to
        populate fewer fields than a later row silently dropped that later
        row's extra values as NULL instead of inserting them.
        """
        t = db.create_table("T", {"name": "TEXT", "qty": "INTEGER", "note": "TEXT"})
        inserted = t.append_rows([
            {"name": "first"},
            {"name": "second", "qty": 5, "note": "hello"},
        ])
        assert inserted == [
            ["first", None, None, 1],
            ["second", 5, "hello", 2],
        ]

    def test_regression_append_rows_preserves_columns_missing_from_first_list_row(self, db):
        """Same bug, list-row form: a shorter first row must not truncate
        the columns available to later, longer rows."""
        t = db.create_table("T", {"name": "TEXT", "qty": "INTEGER", "note": "TEXT"})
        inserted = t.append_rows([
            ["onlyname"],
            ["full", 7, "a note"],
        ])
        assert inserted == [
            ["onlyname", None, None, 1],
            ["full", 7, "a note", 2],
        ]

    def test_append_rows_empty_input_returns_empty_list(self, db, table):
        assert table.append_rows([]) == []
        assert table.length == 0

    def test_append_rows_rejects_unsupported_row_type(self, db, table):
        with pytest.raises(TypeError):
            table.append_rows(["not-a-row-or-dict"])

    def test_append_rows_is_atomic_on_failure(self, db, table, logger):
        """One bad row in a batch must roll back the whole batch, not
        leave a partially-inserted table."""
        result = table.append_rows([
            {"name": "Alice", "age": 30},
            {"no_such_column": "x"},
            {"name": "Carol", "age": 40},
        ])
        assert result == []
        assert table.length == 0
        assert table.read_rows() == []
        assert len(logger.errors) == 1

    def test_regression_delete_row_nonexistent_id_does_not_corrupt_length(self, db, table):
        """
        Regression: delete_row() used to unconditionally do `self.length -= 1`
        before checking whether the DELETE actually matched a row. A
        DELETE ... WHERE ID = ? for a non-existent ID is valid SQL that
        affects zero rows (no exception), so this silently desynced
        `length` from the real row count on every no-op delete.
        """
        table.append_row(["Alice", 30])
        assert table.length == 1

        result = table.delete_row(999_999)

        assert result is True  # the SQL itself executed fine
        assert table.length == 1  # but nothing was actually removed
        assert len(table.read_rows()) == 1

    def test_delete_row_existing_decrements_length(self, db, table):
        row = table.append_row(["Alice", 30])
        assert table.delete_row(row[-1]) is True
        assert table.length == 0
        assert table.read_rows() == []

    def test_delete_rows_only_decrements_by_actual_matches(self, db, table):
        r1 = table.append_row(["Alice", 30])
        table.append_row(["Bob", 25])
        result = table.delete_rows([r1[-1], 999_999, 888_888])
        assert result is True
        assert table.length == 1  # only Alice's row actually matched

    def test_clear_empties_table_and_resets_length(self, db, table):
        table.append_row(["Alice", 30])
        table.append_row(["Bob", 25])
        assert table.clear() is True
        assert table.length == 0
        assert table.read_rows() == []

    def test_assign_row_updates_fields_by_id(self, db, table):
        row = table.append_row(["Alice", 30])
        row_id = row[-1]
        assert table.assign_row(["Alicia", 31, row_id]) is True
        assert table.read_rows()[0] == ["Alicia", 31, row_id]


# ────────────────────────────────────────────────────────────────────────── #
#  Search                                                                     #
# ────────────────────────────────────────────────────────────────────────── #

class TestSearch:
    def _seed(self, db):
        t = db.create_table("T", {"name": "TEXT", "age": "INTEGER"})
        t.append_rows([["Alice", 30], ["Bob", 25], ["Carol", 41]])
        return t

    def test_finds_case_insensitive_substring(self, db):
        t = self._seed(db)
        result = t.search_rows("ali")
        assert [r[0] for r in result] == ["Alice"]

    def test_matches_across_numeric_columns_too(self, db):
        t = self._seed(db)
        result = t.search_rows("41")
        assert [r[0] for r in result] == ["Carol"]

    def test_blank_search_returns_empty_dblist(self, db):
        t = self._seed(db)
        result = t.search_rows("")
        assert len(result) == 0
        assert result.cache == []

    def test_no_match_returns_empty(self, db):
        t = self._seed(db)
        assert len(t.search_rows("zzz_no_such_thing")) == 0

    def test_skips_non_searchable_json_and_blob_columns_without_erroring(self, db):
        t = db.create_table("T", {"name": "TEXT", "meta": "JSON", "blob": "BLOB"})
        t.append_row(["Alice", {"k": "v"}, b"\x00\x01"])
        result = t.search_rows("Alice")  # must not raise despite JSON/BLOB cols
        assert [r[0] for r in result] == ["Alice"]

    def test_build_search_where_blank_returns_no_condition(self, db, table):
        where, params = table._build_search_where("")
        assert where == "" and params == []

    def test_build_search_where_uses_table_alias(self, db, table):
        where, params = table._build_search_where("x", table_alias="a")
        assert "a.[name]" in where or "a.[age]" in where
        assert params  # one LIKE pattern per searchable column

    def test_search_respects_limit_truncation(self, db):
        t = db.create_table("T", {"name": "TEXT"}, limit=2)
        t.append_rows([["match1"], ["match2"], ["match3"]])
        result = t.search_rows("match")
        assert len(result) == 2  # capped at `limit`, not all 3 matches


# ────────────────────────────────────────────────────────────────────────── #
#  Many-to-one relations (setup_fk / set_fk / clear_fk)                       #
# ────────────────────────────────────────────────────────────────────────── #

class TestManyToOne:
    def test_setup_fk_adds_link_id_column(self, db):
        users = db.create_table("Users", {"name": "TEXT"})
        orders = db.create_table("Orders", {"item": "TEXT"})
        orders.setup_fk(users.id)
        assert "link_id" in orders.node_columns
        assert db.get_table_fields("Orders")["link_id"] == "INTEGER"

    def test_setup_fk_is_idempotent(self, db):
        users = db.create_table("Users", {"name": "TEXT"})
        orders = db.create_table("Orders", {"item": "TEXT"})
        orders.setup_fk(users.id)
        orders.setup_fk(users.id)  # must not raise "duplicate column"
        assert orders.node_columns.count("link_id") == 1

    def test_set_fk_and_clear_fk(self, db):
        users = db.create_table("Users", {"name": "TEXT"})
        orders = db.create_table("Orders", {"item": "TEXT"})
        orders.setup_fk(users.id)
        u = users.append_row(["Alice"])
        o = orders.append_row(["Widget"])

        assert orders.set_fk(o[-1], u[-1]) is True
        assert orders.read_rows()[0][-2] == u[-1]  # link_id column

        assert orders.clear_fk(o[-1]) is True
        assert orders.read_rows()[0][-2] is None

    def test_calc_linked_rows_fk_returns_matching_rows(self, db):
        users = db.create_table("Users", {"name": "TEXT"})
        orders = db.create_table("Orders", {"item": "TEXT"})
        orders.setup_fk(users.id)
        u1 = users.append_row(["Alice"])
        u2 = users.append_row(["Bob"])
        o1 = orders.append_row(["Widget"])
        o2 = orders.append_row(["Gadget"])
        orders.set_fk(o1[-1], u1[-1])
        orders.set_fk(o2[-1], u2[-1])

        result = orders.calc_linked_rows_fk([u1[-1]])
        assert [r[0] for r in result] == ["Widget"]

    def test_calc_linked_rows_fk_with_search_filters_further(self, db):
        users = db.create_table("Users", {"name": "TEXT"})
        orders = db.create_table("Orders", {"item": "TEXT"})
        orders.setup_fk(users.id)
        u = users.append_row(["Alice"])
        o1 = orders.append_row(["Widget"])
        o2 = orders.append_row(["Gadget"])
        orders.set_fk(o1[-1], u[-1])
        orders.set_fk(o2[-1], u[-1])

        result = orders.calc_linked_rows_fk([u[-1]], search="Widg")
        assert [r[0] for r in result] == ["Widget"]


# ────────────────────────────────────────────────────────────────────────── #
#  Many-to-many relations (setup_junction / add_link / calc_linked_rows)      #
# ────────────────────────────────────────────────────────────────────────── #

class TestManyToMany:
    def _seed(self, db):
        users = db.create_table("Users", {"name": "TEXT"})
        orders = db.create_table("Orders", {"item": "TEXT"})
        relname, relfields = orders.setup_junction("Users", {"qty": int})
        u1 = users.append_row(["Alice"])
        u2 = users.append_row(["Bob"])
        o1 = orders.append_row(["Widget"])
        o2 = orders.append_row(["Gadget"])
        return users, orders, relname, u1, u2, o1, o2

    def test_setup_junction_creates_table_with_payload_fields(self, db):
        orders = db.create_table("Orders", {"item": "TEXT"})
        relname, fields = orders.setup_junction("Users", {"qty": int})
        assert relname == "Orders2Users"
        assert fields == {"qty": "INTEGER"}
        assert "Orders2Users" in db.table_names

    def test_setup_junction_no_payload_fields(self, db):
        orders = db.create_table("Orders", {"item": "TEXT"})
        relname, fields = orders.setup_junction("Users", {})
        assert fields == {}
        # src_id/tgt_id/ID always exist even with no extra payload.
        assert set(db.get_table_fields(relname, remove_id=False)) == {
            "src_id", "tgt_id", "ID",
        }

    def test_regression_setup_junction_idempotent_when_unchanged(self, db):
        """
        Regression (severe): setup_junction()'s "has the schema actually
        changed?" check compared get_table_fields(relname) -- which always
        includes the junction's own src_id/tgt_id columns -- directly
        against the caller's `fields` dict, which never contains src_id/
        tgt_id (just the extra payload, e.g. {'qty': int}). Those two key
        sets could therefore never be equal, so the "unchanged" branch was
        unreachable: *every* call, even with byte-identical fields, fell
        through to "schema changed" and dropped + recreated the junction
        table, destroying every existing link.

        This matters well beyond a single explicit call: tables.py's
        Table.__init__ calls setup_junction() on every construction of a
        many-to-many-linked Table widget -- i.e. on every screen load --
        so this bug meant many-to-many link data could not survive a
        server restart or a second page load.
        """
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], {"qty": 5}, link_index_name=relname)

        orders.setup_junction("Users", {"qty": int})  # same fields again

        # Table must NOT have been dropped/recreated -- data survives.
        assert db.qlist(f"SELECT qty FROM [{relname}]") == [[5]]

    def test_setup_junction_idempotent_across_repeated_calls(self, db):
        """The bug above would also compound: three identical calls used
        to mean three consecutive silent wipes."""
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], {"qty": 5}, link_index_name=relname)

        for _ in range(3):
            orders.setup_junction("Users", {"qty": int})

        assert db.qlist(f"SELECT qty FROM [{relname}]") == [[5]]

    def test_setup_junction_recreates_when_fields_change(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], {"qty": 5}, link_index_name=relname)

        orders.setup_junction("Users", {"qty": int, "note": str})  # schema changed

        assert set(db.get_table_fields(relname)) == {"src_id", "tgt_id", "qty", "note"}
        assert db.qlist(f"SELECT * FROM [{relname}]") == []  # recreated, data lost

    def test_add_link_returns_full_row_with_id(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        link = orders.add_link(o1[-1], "Users", u1[-1], {"qty": 3}, link_index_name=relname)
        assert link == [o1[-1], u1[-1], 3, 1]  # src, tgt, qty, junction ID

    def test_add_links_bulk(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        links = orders.add_links("Users", [o1[-1], o2[-1]], u1[-1], link_index_name=relname)
        assert len(links) == 2
        assert [l[0] for l in links] == [o1[-1], o2[-1]]
        assert all(l[1] == u1[-1] for l in links)

    def test_delete_link_removes_specific_row(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        link = orders.add_link(o1[-1], "Users", u1[-1], link_index_name=relname)
        assert orders.delete_link("Users", link[-1], index_name=relname) is True
        assert db.qlist(f"SELECT * FROM [{relname}]") == []

    def test_regression_delete_links_empty_list_is_a_noop_not_a_crash(self, db):
        """
        Regression: delete_links(link_ids=[]) used to check `if link_ids:`,
        which is False for an empty list -- identical to link_ids=None --
        so it fell through to the source_ids/link_node_id branch and
        crashed with TypeError('NoneType' object is not iterable) when
        those weren't supplied either, instead of the (correct) no-op.
        """
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], link_index_name=relname)

        result = orders.delete_links("Users", link_ids=[], index_name=relname)

        assert result is True
        assert len(db.qlist(f"SELECT * FROM [{relname}]")) == 1  # nothing deleted

    def test_delete_links_by_ids(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        l1 = orders.add_link(o1[-1], "Users", u1[-1], link_index_name=relname)
        orders.add_link(o2[-1], "Users", u1[-1], link_index_name=relname)

        orders.delete_links("Users", link_ids=[l1[-1]], index_name=relname)

        assert len(db.qlist(f"SELECT * FROM [{relname}]")) == 1

    def test_delete_links_by_source_ids_and_node(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], link_index_name=relname)
        orders.add_link(o2[-1], "Users", u1[-1], link_index_name=relname)
        orders.add_link(o1[-1], "Users", u2[-1], link_index_name=relname)

        orders.delete_links(
            "Users", link_node_id=u1[-1], source_ids=[o1[-1], o2[-1]], index_name=relname,
        )

        remaining = db.qlist(f"SELECT src_id, tgt_id FROM [{relname}]")
        assert remaining == [[o1[-1], u2[-1]]]

    def test_calc_linked_rows_basic(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], {"qty": 3}, link_index_name=relname)
        orders.add_link(o2[-1], "Users", u1[-1], {"qty": 7}, link_index_name=relname)

        result = orders.calc_linked_rows(relname, [u1[-1]], "Users")

        assert [r[0] for r in result] == ["Widget", "Gadget"]
        assert all(len(r) == len(orders._all_columns) for r in result)  # no rel fields

    def test_calc_linked_rows_with_search(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], link_index_name=relname)
        orders.add_link(o2[-1], "Users", u1[-1], link_index_name=relname)

        result = orders.calc_linked_rows(relname, [u1[-1]], "Users", search="Widg")
        assert [r[0] for r in result] == ["Widget"]

    def test_regression_calc_linked_rows_include_rels_true_includes_payload(self, db):
        """
        Regression: calc_linked_rows(..., include_rels=True) added ", r.*"
        to the SELECT (fetching the junction row's own columns) but
        _row_to_list() only ever extracted self._all_columns worth of
        values, so those extra columns were silently discarded --
        include_rels=True and False produced byte-for-byte identical
        results. tables.py's Table(link=...) UI relies on include_rels to
        surface many-to-many payload fields (e.g. a "qty" on the link) --
        this feature was completely inert.
        """
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        l1 = orders.add_link(o1[-1], "Users", u1[-1], {"qty": 3}, link_index_name=relname)
        l2 = orders.add_link(o2[-1], "Users", u1[-1], {"qty": 7}, link_index_name=relname)

        result = orders.calc_linked_rows(relname, [u1[-1]], "Users", include_rels=True)

        # [<node fields...>, <node ID>, <qty>, <junction row ID>]
        assert list(result) == [
            ["Widget", o1[-1], 3, l1[-1]],
            ["Gadget", o2[-1], 7, l2[-1]],
        ]

    def test_calc_linked_rows_include_rels_true_with_no_payload_fields(self, db):
        """A plain many-to-many link with no extra payload columns should
        still append the junction row's own ID when include_rels=True."""
        orders = db.create_table("Orders", {"item": "TEXT"})
        users = db.create_table("Users", {"name": "TEXT"})
        relname, _ = orders.setup_junction("Users", {})
        u = users.append_row(["Alice"])
        o = orders.append_row(["Widget"])
        link = orders.add_link(o[-1], "Users", u[-1], link_index_name=relname)

        result = orders.calc_linked_rows(relname, [u[-1]], "Users", include_rels=True)

        assert list(result) == [["Widget", o[-1], link[-1]]]

    def test_calc_linked_rows_include_rels_false_unaffected(self, db):
        """include_rels defaults to False and must keep returning bare
        node rows, matching the pre-existing (correct) behaviour."""
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], {"qty": 3}, link_index_name=relname)

        result = orders.calc_linked_rows(relname, [u1[-1]], "Users")
        assert list(result) == [["Widget", o1[-1]]]

    def test_on_delete_cascade_removes_junction_rows(self, db):
        users, orders, relname, u1, u2, o1, o2 = self._seed(db)
        orders.add_link(o1[-1], "Users", u1[-1], link_index_name=relname)
        orders.add_link(o2[-1], "Users", u1[-1], link_index_name=relname)
        assert len(db.qlist(f"SELECT * FROM [{relname}]")) == 2

        orders.delete_row(o1[-1])

        remaining = db.qlist(f"SELECT src_id FROM [{relname}]")
        assert remaining == [[o2[-1]]]


# ────────────────────────────────────────────────────────────────────────── #
#  Dbtable._version (the cache-invalidation counter Dblist relies on --      #
#  see dbunits.py's test suite for the consumer side of this contract)        #
# ────────────────────────────────────────────────────────────────────────── #

class TestVersionCounter:
    def test_append_row_bumps_version(self, db, table):
        v0 = table._version
        table.append_row(["Alice", 30])
        assert table._version == v0 + 1

    def test_append_row_failure_does_not_bump_version(self, db, table):
        v0 = table._version
        table.append_row({"no_such_column": "x"})
        assert table._version == v0

    def test_append_rows_bumps_version_once(self, db, table):
        v0 = table._version
        table.append_rows([["Alice", 30], ["Bob", 25]])
        assert table._version == v0 + 1

    def test_append_rows_empty_does_not_bump_version(self, db, table):
        v0 = table._version
        table.append_rows([])
        assert table._version == v0

    def test_delete_row_existing_bumps_version(self, db, table):
        row = table.append_row(["Alice", 30])
        v0 = table._version
        table.delete_row(row[-1])
        assert table._version == v0 + 1

    def test_delete_row_nonexistent_does_not_bump_version(self, db, table):
        table.append_row(["Alice", 30])
        v0 = table._version
        table.delete_row(999_999)
        assert table._version == v0

    def test_delete_rows_bumps_version_only_when_something_matched(self, db, table):
        table.append_row(["Alice", 30])
        v0 = table._version
        table.delete_rows([999_999])
        assert table._version == v0  # nothing matched
        table.delete_rows([1])
        assert table._version == v0 + 1  # one real row matched

    def test_clear_bumps_version(self, db, table):
        table.append_row(["Alice", 30])
        v0 = table._version
        table.clear()
        assert table._version == v0 + 1
