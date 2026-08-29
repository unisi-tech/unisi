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
  TestRowToDict             - Dbtable.row_to_dict (incl. extra_fields, POINT folding)
  TestGetFindOneUpdate      - Dbtable.get/find_one/update (single-row, cache-fresh access)
  TestSearch                - search_rows / _build_search_where
  TestManyToOne             - setup_fk/set_fk/clear_fk/calc_linked_rows_fk
  TestManyToOneSchemaStability - link_id doesn't trigger spurious Schema
                                 Evolution across a restart (mirrors
                                 TestGeoSchemaStability for POINT fields)
  TestManyToMany            - setup_junction/add_link/delete_link(s)/calc_linked_rows
  TestVersionCounter        - Dbtable._version bump semantics (see dbunits.py)
  TestGeoPointFieldType     - [float,float]/(float,float) -> "POINT" detection
  TestGeoPhysicalColumnHelpers - _physical_field_columns/_fold_point_columns/
                                 _expand_point_props/_split_point_value
  TestHaversineKm           - the haversine_km(lat1,lng1,lat2,lng2) function
  TestGeoTableCreation      - create_table's physical _x/_y columns + index
  TestGeoSchemaStability    - POINT schema survives re-declaration/restart
                               without spuriously triggering Schema Evolution
  TestGeoRowCRUD            - append_row(s)/assign_row/update_row/read with POINT
  TestGeoSearch             - search_rows excludes POINT; search_within_radius;
                               search_nearest
  TestGeoSchemaMigration    - Smart Schema Evolution renaming a POINT field
  TestGeoManyToMany         - a POINT payload field on a junction table

Regression tests for bugs found while writing this suite are marked
"Regression:" in their docstring, with a short description of the bug.
"""
import math
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from unisi.db import (
    Database,
    Dbtable,
    GEO_TYPE,
    _adapt_value,
    _convert_value,
    _equal_field_dicts,
    _expand_point_props,
    _fold_point_columns,
    _physical_field_columns,
    _physical_column_names,
    _point_columns,
    _split_point_value,
    haversine_km,
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

    def test_extra_link_id_on_existing_side_is_allowed(self):
        """A many-to-one Table(link=...) always ends up with an on-disk
        link_id column that the caller's freshly-declared `fields` dict
        never mentions (setup_fk() adds it *after* this comparison runs --
        see Table.__init__ in tables.py). Without this allowance every
        restart against an existing linked table looks like a schema
        change when nothing actually changed."""
        existing = {"item": "TEXT", Dbtable.LINK_ID: "INTEGER"}
        fresh = {"item": "TEXT"}
        assert _equal_field_dicts(existing, fresh)

    def test_extra_link_id_does_not_mask_a_real_field_difference(self):
        """The link_id allowance is narrow: a genuinely new/removed field
        alongside it must still be detected."""
        existing = {"item": "TEXT", Dbtable.LINK_ID: "INTEGER"}
        fresh = {"item": "TEXT", "note": "TEXT"}
        assert not _equal_field_dicts(existing, fresh)

    def test_link_id_present_on_both_sides_still_compares_normally(self):
        """If the caller (unusually) does declare link_id explicitly
        themselves, both sides already agree on it -- no special-casing
        needed or applied."""
        existing = {"item": "TEXT", Dbtable.LINK_ID: "INTEGER"}
        fresh = {"item": "TEXT", Dbtable.LINK_ID: "INTEGER"}
        assert _equal_field_dicts(existing, fresh)

    def test_missing_link_id_on_existing_side_is_a_real_difference(self):
        """Only an *extra* link_id on the existing/on-disk side is
        tolerated -- a table that never had one but is now being declared
        with an (unusual, self-declared) link_id field is a genuine
        mismatch, not the restart scenario this allowance exists for."""
        existing = {"item": "TEXT"}
        fresh = {"item": "TEXT", Dbtable.LINK_ID: "INTEGER"}
        assert not _equal_field_dicts(existing, fresh)


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

    def test_index_of_id_returns_zero_based_position(self, db, table):
        r1 = table.append_row(["Alice", 30])
        r2 = table.append_row(["Bob", 25])
        r3 = table.append_row(["Carol", 40])
        assert table.index_of_id(r1[-1]) == 0
        assert table.index_of_id(r2[-1]) == 1
        assert table.index_of_id(r3[-1]) == 2

    def test_regression_index_of_id_accounts_for_gaps_from_deleted_rows(self, db, table):
        """
        Regression: a row's DB id and its position in the default listing
        (ORDER BY ID) only coincide for a table that's never had a row
        deleted -- index_of_id must recompute from what's actually still
        there rather than assuming position == id - 1. This is what
        tables.py's link_table_selection_changed relies on to turn a
        linked row's id into the position it needs for Table.value (see
        tests/units/test_tables.py's matching regression tests).
        """
        r1 = table.append_row(["Alice", 30])
        r2 = table.append_row(["Bob", 25])
        r3 = table.append_row(["Carol", 40])
        table.delete_row(r1[-1])
        assert table.index_of_id(r2[-1]) == 0  # Bob is now first
        assert table.index_of_id(r3[-1]) == 1

    def test_index_of_id_for_an_id_past_the_end_counts_every_row(self, db, table):
        table.append_row(["Alice", 30])  # id 1
        table.append_row(["Bob", 25])    # id 2
        assert table.index_of_id(999) == 2


# ────────────────────────────────────────────────────────────────────────── #
#  Single-row access: get / find_one / update / row_to_dict                   #
# ────────────────────────────────────────────────────────────────────────── #

class TestRowToDict:
    def test_labels_fields_and_appends_id(self, db, table):
        row = table.append_row(["Alice", 30])
        assert table.row_to_dict(row) == {"name": "Alice", "age": 30, "id": row[-1]}

    def test_extra_fields_labelled_in_order_after_id(self, db, table):
        row = table.append_row(["Alice", 30])
        tagged = [*row, 4.2, "bonus"]  # e.g. distance_km, then something else
        result = table.row_to_dict(tagged, extra_fields=("distance_km", "note"))
        assert result == {
            "name": "Alice", "age": 30, "id": row[-1],
            "distance_km": 4.2, "note": "bonus",
        }

    def test_no_extra_fields_by_default(self, db, table):
        row = table.append_row(["Alice", 30])
        assert "distance_km" not in table.row_to_dict(row)

    def test_folds_a_point_field_back_into_one_entry(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [13.7563, 100.5018]})
        assert t.row_to_dict(row) == {
            "name": "A", "pos": [13.7563, 100.5018], "id": row[-1],
        }


class TestGetFindOneUpdate:
    def test_get_returns_a_labelled_dict(self, db, table):
        row = table.append_row(["Alice", 30])
        assert table.get(row[-1]) == {"name": "Alice", "age": 30, "id": row[-1]}

    def test_get_missing_id_returns_none(self, db, table):
        assert table.get(999999) is None

    def test_get_reads_fresh_not_from_a_stale_list_cache(self, db, table):
        """The whole point of get(): unlike table.list (a Dblist view that
        is only as fresh as the last operation that bumped
        Dbtable._version), it never reads a cached chunk -- see
        test_dbunits.py::TestDirectDbtableBypass::
        test_regression_direct_update_row_is_not_served_stale for the
        underlying cache-staleness bug this sidesteps entirely by never
        touching the cache in the first place."""
        row = table.append_row(["Alice", 30])
        row_id = row[-1]
        list(table.list)  # populate the Dblist cache

        db.update_row("T", row_id, {"age": 99})

        assert table.get(row_id)["age"] == 99

    def test_find_one_matches_a_single_field(self, db, table):
        table.append_row(["Alice", 30])
        table.append_row(["Bob", 25])
        assert table.find_one(name="Bob") == {"name": "Bob", "age": 25, "id": 2}

    def test_find_one_matches_multiple_fields_combined_with_and(self, db, table):
        table.append_row(["Alice", 30])
        table.append_row(["Alice", 40])
        result = table.find_one(name="Alice", age=40)
        assert result["age"] == 40

    def test_find_one_no_match_returns_none(self, db, table):
        table.append_row(["Alice", 30])
        assert table.find_one(name="Zoe") is None

    def test_find_one_is_an_exact_match_not_a_substring(self, db, table):
        """The reason find_one() exists instead of reusing search_rows():
        search_rows() is a case-insensitive *substring* match across every
        text column -- 'repair' would match both 'repair' and
        'computer_repair'. find_one() must not."""
        table.append_row(["computer_repair", 1])
        assert table.find_one(name="repair") is None
        assert table.find_one(name="computer_repair") is not None

    def test_find_one_values_are_parameterised_not_interpolated(self, db, table):
        """A value containing SQL-special characters must be treated as
        literal data, not syntax -- proof that field values go through the
        parameter list rather than being formatted into the query text."""
        table.append_row(["O'Brien", 30])
        assert table.find_one(name="O'Brien")["age"] == 30

    def test_update_patches_only_the_given_fields(self, db, table):
        row = table.append_row(["Alice", 30])
        result = table.update(row[-1], {"age": 31})
        assert result == {"name": "Alice", "age": 31, "id": row[-1]}

    def test_update_persists_to_the_database(self, db, table):
        row = table.append_row(["Alice", 30])
        table.update(row[-1], {"age": 31})
        assert db.qlist("SELECT age FROM [T] WHERE ID = ?", (row[-1],)) == [[31]]

    def test_update_missing_id_returns_none(self, db, table):
        assert table.update(999999, {"age": 31}) is None

    def test_update_bumps_version_so_other_dblist_reads_see_it(self, db, table):
        """Regression: see test_dbunits.py::TestDirectDbtableBypass::
        test_regression_direct_update_row_is_not_served_stale -- update()
        goes through Database.update_row(), which now bumps
        Dbtable._version on a successful write, same as append_row/
        delete_row already did."""
        row = table.append_row(["Alice", 30])
        list(table.list)  # populate the cache

        table.update(row[-1], {"age": 31})

        assert list(table.list)[0][1] == 31

    def test_update_expands_a_point_field(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [1.0, 2.0]})
        result = t.update(row[-1], {"pos": [3.0, 4.0]})
        assert result["pos"] == [3.0, 4.0]


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
#  Many-to-one Smart Schema Evolution stability                               #
# ────────────────────────────────────────────────────────────────────────── #

class TestManyToOneSchemaStability:
    """
    Regression (severe, restart-breaking): get_table()'s schema comparison
    (_equal_field_dicts) runs against the caller's plain `fields=` dict,
    which never mentions link_id -- that column is only added afterwards,
    by an explicit setup_fk() call once the Dbtable already exists (see
    Table.__init__ in tables.py: it calls Unishare.db.set_db_list(self) --
    i.e. get_table() -- *before* its `match self.link:` block reaches
    self.rows.dbtable.setup_fk(...)). On a table's very first creation this
    is harmless (get_table_fields() returns None, so the comparison is
    skipped entirely). On every subsequent call, though, the on-disk schema
    legitimately *does* have link_id (added last time), the freshly-passed
    `fields` dict never will, and without the allowance added to
    _equal_field_dicts, Smart Schema Evolution's interactive console prompt
    (input()) fires -- destructively offering to drop the table -- on
    every single restart of every app with any Table(link=...), forever,
    even though nothing about the declared fields ever changed.

    Mirrors TestGeoSchemaStability's three-test shape exactly (in-process
    re-declaration, no console output, real two-connection restart) -- the
    analogous fix for the analogous problem with a different
    framework-managed column (link_id vs POINT's physical _x/_y pair).
    """

    def _linked_orders(self, db, fields=None):
        """Simulates tables.py's actual Table.__init__ sequence for a
        Table(link=...): get_table() first (fields never include link_id),
        setup_fk() afterwards -- not db.create_table()+setup_fk(), which
        (as TestManyToOne already covers) bypasses get_table()'s
        comparison entirely and so can't exercise this bug."""
        fields = fields or {"item": str}
        users = db.create_table("Users", {"name": str})
        orders = db.get_table("Orders", fields=fields)
        orders.setup_fk(users.id)
        return orders

    def test_redeclaring_a_linked_table_does_not_invoke_migration(
        self, db, monkeypatch
    ):
        fields = {"item": str}
        self._linked_orders(db, fields)

        def fail_if_called(prompt=""):
            raise AssertionError(
                "Smart Schema Evolution fired on an unchanged link= schema"
            )
        monkeypatch.setattr("builtins.input", fail_if_called)

        # "Restart": get_table() called again with the SAME fields -- the
        # caller still doesn't (and never does) declare link_id explicitly.
        orders2 = db.get_table("Orders", fields=fields)

        assert "link_id" in orders2.node_columns
        assert not any(n.startswith("Orders_OLD_") for n in db.table_names)

    def test_redeclaring_a_linked_table_produces_no_prompt_output(
        self, db, capsys
    ):
        fields = {"item": str}
        self._linked_orders(db, fields)
        capsys.readouterr()  # discard anything printed by table/FK setup

        db.get_table("Orders", fields=fields)

        assert "SCHEMA CHANGE DETECTED" not in capsys.readouterr().out

    def test_a_real_schema_change_alongside_link_id_still_migrates(
        self, db, monkeypatch
    ):
        """The allowance is narrow: adding a genuinely new field to an
        already-linked table must still trigger migration, exactly as for
        any other table."""
        self._linked_orders(db, {"item": str})
        monkeypatch.setattr("builtins.input", lambda prompt="": "1")  # cancel

        result = db.get_table("Orders", fields={"item": str, "note": str})

        assert "note" not in result.node_columns  # cancelled -- old schema kept
        assert "link_id" in result.node_columns

    def test_survives_a_real_process_restart_on_a_file_backed_db(
        self, tmp_path, logger
    ):
        """The same guarantee as above, but across two independent
        Database connections against the same on-disk file -- the actual
        shape of an application restart, not just a second in-process
        call."""
        dbpath = str(tmp_path / "app.db")
        fields = {"item": str}

        db1 = Database(dbpath, message_logger=logger)
        try:
            users = db1.create_table("Users", {"name": str})
            orders = db1.get_table("Orders", fields=fields)
            orders.setup_fk(users.id)
            u = users.append_row(["Alice"])
            o = orders.append_row(["Widget"])
            orders.set_fk(o[-1], u[-1])
        finally:
            db1.close()

        db2 = Database(dbpath, message_logger=logger)
        try:
            import builtins
            original_input = builtins.input

            def fail_if_called(prompt=""):
                raise AssertionError("migration prompt fired across a restart")
            builtins.input = fail_if_called
            try:
                orders2 = db2.get_table("Orders", fields=fields)
            finally:
                builtins.input = original_input

            assert orders2.read_rows()[0][0] == "Widget"
            assert "link_id" in orders2.node_columns
        finally:
            db2.close()


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


# ────────────────────────────────────────────────────────────────────────── #
#  Geo-spatial (POINT) fields                                                #
#                                                                             #
#  A field declared as {'position': [float, float]} / (float, float) is     #
#  auto-detected as a 2D point and stored as two physical REAL columns,      #
#  {name}_x / {name}_y (x=longitude, y=latitude), indexed together for a     #
#  bounding-box pre-filter. Everywhere outside db.py itself, it's still      #
#  exactly one logical field with one [x, y] cell value.                    #
# ────────────────────────────────────────────────────────────────────────── #

class TestGeoPointFieldType:
    """[float, float] / (float, float) field-spec detection -- the same
    auto-detection TestNormalizeFieldTypes covers for every other type."""

    def test_list_of_two_floats_is_point(self):
        assert normalize_field_types({"pos": [float, float]}) == {"pos": "POINT"}

    def test_tuple_of_two_floats_is_point(self):
        assert normalize_field_types({"pos": (float, float)}) == {"pos": "POINT"}

    def test_mixed_int_float_is_point(self):
        # Coordinates are inherently continuous; int is accepted alongside
        # float for convenience but is still stored/returned as REAL.
        assert normalize_field_types({"pos": [int, float]}) == {"pos": "POINT"}
        assert normalize_field_types({"pos": (int, int)}) == {"pos": "POINT"}

    def test_point_mixed_with_ordinary_fields(self):
        result = normalize_field_types(
            {"name": str, "pos": [float, float], "age": int}
        )
        assert result == {"name": "TEXT", "pos": "POINT", "age": "INTEGER"}

    def test_plain_list_type_is_still_json_not_point(self):
        # {'tags': list} (the *type* list, not a 2-element container of
        # types) must keep meaning "arbitrary JSON array", unchanged.
        assert normalize_field_types({"tags": list}) == {"tags": "JSON"}
        assert normalize_field_types({"tags": tuple}) == {"tags": "JSON"}

    def test_wrong_element_types_raises(self):
        with pytest.raises(TypeError):
            normalize_field_types({"bad": [str, str]})

    def test_wrong_length_raises(self):
        with pytest.raises(TypeError):
            normalize_field_types({"bad": [float, float, float]})
        with pytest.raises(TypeError):
            normalize_field_types({"bad": [float]})

    def test_non_type_non_str_spec_still_raises(self):
        # Unrelated regression guard: the new list/tuple branch in
        # normalize_field_types() must not swallow the pre-existing
        # "unsupported spec" error for non-list/tuple garbage.
        with pytest.raises(TypeError):
            normalize_field_types({"age": 42})


class TestGeoPhysicalColumnHelpers:
    """The logical <-> physical translation helpers, tested directly."""

    def test_point_columns_naming(self):
        assert _point_columns("position") == ("position_x", "position_y")

    def test_physical_field_columns_expands_point(self):
        cols = _physical_field_columns({"name": "TEXT", "pos": "POINT"})
        assert cols == [
            ("name", "TEXT"), ("pos_x", "REAL_X"), ("pos_y", "REAL_Y"),
        ]

    def test_physical_field_columns_passthrough_when_no_point(self):
        cols = _physical_field_columns({"name": "TEXT", "age": "INTEGER"})
        assert cols == [("name", "TEXT"), ("age", "INTEGER")]

    def test_physical_column_names_expands_point_in_a_name_list(self):
        fields = {"name": "TEXT", "pos": "POINT"}
        assert _physical_column_names(["pos", "name"], fields) == [
            "pos_x", "pos_y", "name",
        ]

    def test_fold_point_columns_round_trips_physical_field_columns(self):
        logical = {"name": "TEXT", "pos": "POINT", "age": "INTEGER"}
        physical = _physical_field_columns(logical)
        assert _fold_point_columns(physical) == logical

    def test_fold_point_columns_does_not_mis_fold_plain_real_xy_names(self):
        # Only the REAL_X/REAL_Y declared-type marker folds a pair -- an
        # ordinary REAL column that happens to be named "foo_x" is left
        # alone, even if a sibling "foo_y" REAL column also exists.
        raw = [("foo_x", "REAL"), ("foo_y", "REAL")]
        assert _fold_point_columns(raw) == {"foo_x": "REAL", "foo_y": "REAL"}

    def test_fold_point_columns_requires_both_halves(self):
        # An _x half with no matching REAL_Y sibling is not a point field.
        raw = [("pos_x", "REAL_X"), ("other", "TEXT")]
        assert _fold_point_columns(raw) == {"pos_x": "REAL_X", "other": "TEXT"}

    def test_split_point_value_unpacks(self):
        assert _split_point_value([1.0, 2.0]) == (1.0, 2.0)
        assert _split_point_value((1.0, 2.0)) == (1.0, 2.0)

    def test_split_point_value_none_is_none_none(self):
        assert _split_point_value(None) == (None, None)

    def test_split_point_value_bad_shape_raises_value_error(self):
        with pytest.raises(ValueError):
            _split_point_value(5.0)
        with pytest.raises(ValueError):
            _split_point_value([1.0, 2.0, 3.0])

    def test_expand_point_props_splits_matching_keys(self):
        point_fields = {"pos": ("pos_x", "pos_y")}
        expanded = _expand_point_props({"pos": [1.0, 2.0], "name": "A"}, point_fields)
        assert expanded == {"pos_x": 1.0, "pos_y": 2.0, "name": "A"}

    def test_expand_point_props_none_value_nulls_both_columns(self):
        point_fields = {"pos": ("pos_x", "pos_y")}
        expanded = _expand_point_props({"pos": None}, point_fields)
        assert expanded == {"pos_x": None, "pos_y": None}

    def test_expand_point_props_no_point_fields_is_passthrough(self):
        props = {"name": "A", "age": 1}
        assert _expand_point_props(props, {}) is props


class TestHaversineKm:
    def test_same_point_is_zero(self):
        assert haversine_km(13.75, 100.5, 13.75, 100.5) == 0.0

    def test_known_city_distance_within_tolerance(self):
        # Bangkok <-> Chiang Mai, commonly cited as ~580-590 km great-circle.
        d = haversine_km(13.7563, 100.5018, 18.7883, 98.9853)
        assert 560 < d < 610

    def test_none_argument_returns_none(self):
        assert haversine_km(None, 100.5, 13.75, 100.5) is None
        assert haversine_km(13.75, 100.5, None, None) is None

    def test_symmetric(self):
        a = haversine_km(13.75, 100.5, 18.79, 98.99)
        b = haversine_km(18.79, 98.99, 13.75, 100.5)
        assert a == pytest.approx(b)


class TestGeoTableCreation:
    def test_creates_physical_x_y_columns(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        cur = db._conn.execute("PRAGMA table_info('T')")
        cols = {row["name"]: row["type"] for row in cur.fetchall()}
        assert cols["pos_x"] == "REAL_X"
        assert cols["pos_y"] == "REAL_Y"
        assert "pos" not in cols  # no literal "pos" column exists physically

    def test_creates_composite_geo_index(self, db):
        db.create_table("T", {"name": str, "pos": [float, float]})
        cur = db._conn.execute("PRAGMA index_list('T')")
        names = [row["name"] for row in cur.fetchall()]
        assert "T_pos_geo_idx" in names
        cur = db._conn.execute("PRAGMA index_info('T_pos_geo_idx')")
        indexed = [row["name"] for row in cur.fetchall()]
        assert indexed == ["pos_y", "pos_x"]  # (lat, lng) order

    def test_dbtable_exposes_logical_point_field(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        assert t.table_fields == {"name": "TEXT", "pos": "POINT"}
        assert t.node_columns == ["name", "pos"]  # one logical column
        assert t.point_fields == {"pos": ("pos_x", "pos_y")}

    def test_no_index_created_without_a_point_field(self, db):
        db.create_table("T", {"name": str})
        cur = db._conn.execute("PRAGMA index_list('T')")
        assert cur.fetchall() == []

    def test_multiple_point_fields_each_get_own_columns_and_index(self, db):
        t = db.create_table(
            "T", {"from_pos": [float, float], "to_pos": [float, float]}
        )
        assert t.point_fields == {
            "from_pos": ("from_pos_x", "from_pos_y"),
            "to_pos": ("to_pos_x", "to_pos_y"),
        }
        cur = db._conn.execute("PRAGMA index_list('T')")
        names = {row["name"] for row in cur.fetchall()}
        assert {"T_from_pos_geo_idx", "T_to_pos_geo_idx"} <= names


class TestGeoSchemaStability:
    """
    Regression (severe, restart-breaking): get_table_fields() used to
    return the *physical* schema verbatim -- e.g. {'pos_x': 'REAL_X',
    'pos_y': 'REAL_Y'} for a POINT field -- which could never compare
    equal to the freshly normalised *logical* fields dict
    ({'pos': 'POINT'}), even when the declared schema had not actually
    changed at all. Smart Schema Evolution's interactive console prompt
    would therefore fire on *every* get_table() call for a table with a
    POINT field -- including the second call in the same process, and
    every subsequent app restart -- destructively offering to drop the
    table each time. get_table_fields() now folds physical _x/_y pairs
    back into one logical POINT entry so re-declaring the same schema
    compares equal again.
    """

    def test_redeclaring_the_same_point_schema_does_not_invoke_migration(
        self, db, monkeypatch
    ):
        fields = {"name": str, "pos": [float, float]}
        t1 = db.get_table("T", fields=fields)
        t1.append_row({"name": "A", "pos": [1.0, 2.0]})

        def fail_if_called(prompt=""):
            raise AssertionError(
                "Smart Schema Evolution fired on an unchanged POINT schema"
            )
        monkeypatch.setattr("builtins.input", fail_if_called)

        t2 = db.get_table("T", fields=fields)

        assert t2.table_fields == {"name": "TEXT", "pos": "POINT"}
        assert t2.read_rows() == [["A", [1.0, 2.0], 1]]
        assert not any(n.startswith("T_OLD_") for n in db.table_names)

    def test_redeclaring_the_same_point_schema_produces_no_prompt_output(
        self, db, capsys
    ):
        fields = {"name": str, "pos": [float, float]}
        db.get_table("T", fields=fields)
        capsys.readouterr()  # discard anything printed by table creation

        db.get_table("T", fields=fields)

        assert "SCHEMA CHANGE DETECTED" not in capsys.readouterr().out

    def test_binds_to_existing_point_table_without_declaring_fields(self, db):
        db.get_table("T", fields={"name": str, "pos": [float, float]})

        rebound = db.get_table("T")  # Variant 3: id only, no fields=

        assert rebound.table_fields["pos"] == "POINT"
        assert rebound.point_fields == {"pos": ("pos_x", "pos_y")}

    def test_survives_a_real_process_restart_on_a_file_backed_db(
        self, tmp_path, logger
    ):
        """The same guarantee as above, but across two independent
        Database connections against the same on-disk file -- the actual
        shape of an application restart, not just a second in-process call."""
        dbpath = str(tmp_path / "app.db")
        fields = {"name": str, "pos": [float, float]}

        db1 = Database(dbpath, message_logger=logger)
        try:
            t1 = db1.get_table("T", fields=fields)
            t1.append_row({"name": "A", "pos": [13.7563, 100.5018]})
        finally:
            db1.close()

        db2 = Database(dbpath, message_logger=logger)
        try:
            import builtins
            original_input = builtins.input

            def fail_if_called(prompt=""):
                raise AssertionError("migration prompt fired across a restart")
            builtins.input = fail_if_called
            try:
                t2 = db2.get_table("T", fields=fields)
            finally:
                builtins.input = original_input

            assert t2.read_rows() == [["A", [13.7563, 100.5018], 1]]
        finally:
            db2.close()


class TestGeoRowCRUD:
    def test_append_row_dict_with_list_value(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [100.5, 13.75]})
        assert row == ["A", [100.5, 13.75], 1]

    def test_append_row_dict_with_tuple_value(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": (100.5, 13.75)})
        assert row == ["A", [100.5, 13.75], 1]

    def test_append_row_list_form(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row(["A", [100.5, 13.75]])
        assert row == ["A", [100.5, 13.75], 1]

    def test_append_row_omitted_point_defaults_to_none(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A"})
        assert row == ["A", None, 1]
        cur = db._conn.execute("SELECT pos_x, pos_y FROM T WHERE ID=1")
        assert dict(cur.fetchone()) == {"pos_x": None, "pos_y": None}

    def test_append_row_invalid_point_shape_raises(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        with pytest.raises(ValueError):
            t.append_row({"name": "A", "pos": [1.0, 2.0, 3.0]})

    def test_append_rows_bulk_mixed_dict_and_none(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        rows = t.append_rows([
            {"name": "A", "pos": [1.0, 2.0]},
            {"name": "B", "pos": None},
            {"name": "C", "pos": (3.0, 4.0)},
        ])
        assert rows == [
            ["A", [1.0, 2.0], 1],
            ["B", None, 2],
            ["C", [3.0, 4.0], 3],
        ]

    def test_read_rows_round_trips_point_values(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        t.append_rows([{"name": "A", "pos": [1.5, 2.5]}, {"name": "B", "pos": None}])
        assert t.read_rows() == [["A", [1.5, 2.5], 1], ["B", None, 2]]

    def test_assign_row_updates_point_value(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [1.0, 2.0]})
        row[1] = [9.0, 9.5]
        assert t.assign_row(row) is True
        assert t.read_rows() == [["A", [9.0, 9.5], 1]]

    def test_assign_row_nulls_out_point_value(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [1.0, 2.0]})
        row[1] = None
        t.assign_row(row)
        cur = db._conn.execute("SELECT pos_x, pos_y FROM T WHERE ID=1")
        assert dict(cur.fetchone()) == {"pos_x": None, "pos_y": None}

    def test_update_row_directly_with_point_prop(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [1.0, 2.0]})
        assert db.update_row("T", row[-1], {"pos": [7.0, 8.0]}) is True
        assert t.read_rows() == [["A", [7.0, 8.0], 1]]

    def test_delete_row_with_point_field_present(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        row = t.append_row({"name": "A", "pos": [1.0, 2.0]})
        assert t.delete_row(row[-1]) is True
        assert t.read_rows() == []


class TestGeoSearch:
    def _seed(self, db):
        t = db.create_table(
            "T", {"title": str, "pos": [float, float], "status": str}
        )
        # x=lng, y=lat around central Bangkok (13.7563, 100.5018).
        t.append_rows([
            {"title": "Solo point",  "pos": [100.49,   13.75],   "status": "ACTIVE"},   # ~1.45km
            {"title": "Silom",       "pos": [100.5325, 13.7248], "status": "ACTIVE"},   # ~4.82km
            {"title": "Chatuchak",   "pos": [100.5501, 13.8022], "status": "ACTIVE"},   # ~7.30km
            {"title": "Chiang Mai",  "pos": [98.9853,  18.7883], "status": "ACTIVE"},   # ~583km
            {"title": "No position", "pos": None,                "status": "ACTIVE"},
            {"title": "Inactive",    "pos": [100.5018, 13.7563], "status": "INACTIVE"}, # 0km, filtered out
        ])
        return t

    def test_search_rows_excludes_point_column_without_erroring(self, db):
        t = self._seed(db)
        result = t.search_rows("Silom")
        assert [r[0] for r in result] == ["Silom"]

    def test_search_rows_does_not_match_stray_coordinate_digits(self, db):
        t = self._seed(db)
        # 5325 appears inside Silom's longitude (100.5325) but POINT columns
        # must never be reachable via text search.
        assert len(t.search_rows("5325")) == 0

    def test_within_radius_excludes_farther_points(self, db):
        t = self._seed(db)
        result = t.search_within_radius("pos", 13.7563, 100.5018, 6.0)
        titles = [r[0] for r in result]
        assert "Chatuchak" not in titles     # ~7.3km > 6km
        assert "Chiang Mai" not in titles    # ~583km > 6km
        assert "No position" not in titles   # NULL never matches BETWEEN

    def test_within_radius_includes_closer_points_nearest_first(self, db):
        t = self._seed(db)
        result = t.search_within_radius("pos", 13.7563, 100.5018, 6.0)
        titles = [r[0] for r in result if r[0] != "Inactive"]
        assert titles == ["Solo point", "Silom"]  # ascending distance

    def test_within_radius_appends_trailing_distance_km(self, db):
        t = self._seed(db)
        result = t.search_within_radius("pos", 13.7563, 100.5018, 6.0)
        row = next(r for r in result if r[0] == "Solo point")
        assert len(row) == len(t._all_columns) + 1
        assert row[-1] == pytest.approx(haversine_km(13.7563, 100.5018, 13.75, 100.49))

    def test_within_radius_extra_where_and_params(self, db):
        t = self._seed(db)
        # "Inactive" sits exactly at the query centre (0km) but is ACTIVE-
        # filtered out; without the filter it would rank first.
        result = t.search_within_radius(
            "pos", 13.7563, 100.5018, 6.0, where="status = ?", params=("ACTIVE",)
        )
        assert "Inactive" not in [r[0] for r in result]

    def test_within_radius_unknown_field_raises_value_error(self, db):
        t = self._seed(db)
        with pytest.raises(ValueError):
            t.search_within_radius("title", 13.7563, 100.5018, 5.0)

    def test_within_radius_respects_limit(self, db):
        t = self._seed(db)
        result = t.search_within_radius("pos", 13.7563, 100.5018, 1000.0, limit=2)
        assert len(result) == 2

    def test_nearest_returns_k_closest_ordered(self, db):
        t = self._seed(db)
        result = t.search_nearest("pos", 13.7563, 100.5018, k=2, where="status = ?", params=("ACTIVE",))
        assert [r[0] for r in result] == ["Solo point", "Silom"]

    def test_nearest_expands_radius_to_satisfy_k(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        t.append_rows([
            {"name": "near", "pos": [100.5018, 13.7563]},
            {"name": "far",  "pos": [100.90,   13.7563]},   # ~43km east
        ])
        result = t.search_nearest(
            "pos", 13.7563, 100.5018, k=2, initial_radius_km=1.0, max_radius_km=100.0
        )
        assert [r[0] for r in result] == ["near", "far"]

    def test_nearest_returns_fewer_than_k_when_capped(self, db):
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        t.append_rows([
            {"name": "near", "pos": [100.5018, 13.7563]},
            {"name": "far",  "pos": [100.90,   13.7563]},   # ~43km east
        ])
        result = t.search_nearest(
            "pos", 13.7563, 100.5018, k=2, initial_radius_km=1.0, max_radius_km=5.0
        )
        assert [r[0] for r in result] == ["near"]

    def test_geo_search_at_scale_uses_the_index_not_a_full_scan(self, db):
        """Not a timing assertion (flaky in CI) -- asserts the query plan
        actually uses the composite geo index for the bounding-box
        pre-filter, which is *why* it stays fast at scale."""
        t = db.create_table("T", {"name": str, "pos": [float, float]})
        t.append_rows([
            {"name": f"n{i}", "pos": [100.0 + i * 0.0001, 13.0 + i * 0.0001]}
            for i in range(500)
        ])
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM T "
            "WHERE pos_y BETWEEN ? AND ? AND pos_x BETWEEN ? AND ?",
            (13.0, 13.01, 100.0, 100.01),
        ).fetchall()
        assert any("T_pos_geo_idx" in row["detail"] for row in plan)


class TestGeoSchemaMigration:
    """Smart Schema Evolution with a POINT field involved."""

    def test_fuzzy_renamed_point_field_preserves_coordinates(self, db, monkeypatch):
        # 'geo_position' is lexically close enough to 'position' for
        # difflib's default cutoff (0.6) to offer it as a fuzzy match.
        t = db.get_table("T", fields={"name": str, "position": [float, float]})
        t.append_row({"name": "A", "position": [10.0, 20.0]})
        monkeypatch.setattr("builtins.input", lambda prompt="": "4")

        result = db.get_table(
            "T", fields={"name": str, "geo_position": [float, float]}
        )

        assert result.point_fields == {"geo_position": ("geo_position_x", "geo_position_y")}
        assert result.read_rows() == [["A", [10.0, 20.0], 1]]

    def test_point_field_added_fresh_defaults_to_none(self, db, monkeypatch):
        t = db.get_table("T", fields={"name": str})
        t.append_row(["Alice"])
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")

        result = db.get_table("T", fields={"name": str, "pos": [float, float]})

        assert result.read_rows() == [["Alice", None, 1]]

    def test_point_field_dropped_on_recreate(self, db, monkeypatch):
        t = db.get_table("T", fields={"name": str, "pos": [float, float]})
        t.append_row({"name": "Alice", "pos": [1.0, 2.0]})
        monkeypatch.setattr("builtins.input", lambda prompt="": "2")

        result = db.get_table("T", fields={"name": str})

        assert result.table_fields == {"name": "TEXT"}
        assert result.point_fields == {}
        assert db.qlist("SELECT * FROM T") == []


class TestGeoManyToMany:
    """A POINT payload field on a many-to-many junction table."""

    def _seed(self, db):
        couriers = db.create_table("Couriers", {"name": str})
        zones = db.create_table("Zones", {"name": str})
        relname, fields = couriers.setup_junction(
            "Zones", {"meeting_point": [float, float], "note": str}
        )
        c1 = couriers.append_row({"name": "Bob"})
        z1 = zones.append_row({"name": "Downtown"})
        return couriers, zones, relname, c1, z1

    def test_setup_junction_creates_physical_xy_columns(self, db):
        couriers, zones, relname, c1, z1 = self._seed(db)
        cur = db._conn.execute(f"PRAGMA table_info('{relname}')")
        cols = {row["name"]: row["type"] for row in cur.fetchall()}
        assert cols["meeting_point_x"] == "REAL_X"
        assert cols["meeting_point_y"] == "REAL_Y"

    def test_setup_junction_reports_logical_point_field(self, db):
        couriers, zones, relname, c1, z1 = self._seed(db)
        _, fields = couriers.setup_junction(
            "Zones", {"meeting_point": [float, float], "note": str}
        )
        assert fields == {"meeting_point": "POINT", "note": "TEXT"}

    def test_add_link_splits_point_payload(self, db):
        couriers, zones, relname, c1, z1 = self._seed(db)
        couriers.add_link(
            c1[-1], "Zones", z1[-1],
            {"meeting_point": [100.5, 13.75], "note": "front gate"},
            relname,
        )
        cur = db._conn.execute(
            f"SELECT meeting_point_x, meeting_point_y, note FROM [{relname}]"
        )
        assert dict(cur.fetchone()) == {
            "meeting_point_x": 100.5, "meeting_point_y": 13.75, "note": "front gate",
        }

    def test_calc_linked_rows_include_rels_recomposes_point_payload(self, db):
        couriers, zones, relname, c1, z1 = self._seed(db)
        couriers.add_link(
            c1[-1], "Zones", z1[-1],
            {"meeting_point": [100.5, 13.75], "note": "front gate"},
            relname,
        )

        linked = couriers.calc_linked_rows(relname, [z1[-1]], "Zones", include_rels=True)

        assert list(linked) == [["Bob", 1, [100.5, 13.75], "front gate", 1]]

    def test_calc_linked_rows_works_when_self_also_has_a_point_field(self, db):
        # Exercises the n_self physical-column-count fix directly: self
        # (Couriers) *also* has a POINT field, so the aliased "a.*"-style
        # column count in the JOIN differs from len(self._all_columns).
        couriers = db.create_table("Couriers", {"name": str, "base": [float, float]})
        zones = db.create_table("Zones", {"name": str})
        relname, _ = couriers.setup_junction("Zones", {"note": str})
        c1 = couriers.append_row({"name": "Bob", "base": [1.0, 2.0]})
        z1 = zones.append_row({"name": "Downtown"})
        couriers.add_link(c1[-1], "Zones", z1[-1], {"note": "hi"}, relname)

        linked = couriers.calc_linked_rows(relname, [z1[-1]], "Zones", include_rels=True)

        assert list(linked) == [["Bob", [1.0, 2.0], 1, "hi", 1]]
