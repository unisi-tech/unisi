# Copyright © 2024 UNISI Tech. All rights reserved.
"""
db.py — The single, built-in SQLite database module for UNISI.

Contains:
  • Type system, adapters, and converters for extended Python ↔ SQLite types.
  • Smart Schema Evolution (interactive_migration_choice) for safe migrations.
  • Database and Dbtable classes.
  • Auto-initialisation: ``from unisi.db import db`` yields a ready-to-use
    Database instance (or None when no path is configured).

Usage:
    from unisi.db import db

Type support
────────────
  Python type    │ Declared column type │ Storage
  ───────────────┼──────────────────────┼─────────────────────────────
  bool           │ BOOLEAN              │ INTEGER 0 / 1
  int            │ INTEGER              │ native
  float          │ REAL                 │ native
  str            │ TEXT                 │ native
  bytes          │ BLOB                 │ native
  datetime       │ TIMESTAMP            │ ISO-8601 TEXT
  date           │ DATE                 │ ISO-8601 TEXT
  list / tuple   │ JSON                 │ json.dumps TEXT
  dict           │ JSON                 │ json.dumps TEXT
  Decimal        │ DECIMAL              │ str round-trip TEXT
  uuid.UUID      │ UUID                 │ str round-trip TEXT

Relation support
────────────────
Relations are junction tables:
  CREATE TABLE {from}2{to} (
      src_id INTEGER REFERENCES [{from}](ID) ON DELETE CASCADE,
      tgt_id INTEGER REFERENCES [{to}](ID)  ON DELETE CASCADE,
      ID     INTEGER PRIMARY KEY AUTOINCREMENT,
      <optional extra fields>
  )
ON DELETE CASCADE ensures that deleting a node row automatically removes
its outgoing and incoming links.

Variable-count IN clauses
─────────────────────────
SQLite limits bind variables per statement to SQLITE_MAX_VARIABLE_NUMBER
(32 766 in Python's bundled SQLite ≥ 3.32, 999 in older builds).
calc_linked_rows / delete_links accept arbitrary iterables of IDs; callers
with very large sets (> ~1000 on older SQLite) should batch externally.
"""

import difflib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .dbunits import Dblist


# ── type system ───────────────────────────────────────────────────────────────

def sqlite_data_type(value: Any) -> str:
    """Map a Python sample value to its declared SQLite column type."""
    match value:
        case bool():            return "BOOLEAN"
        case int():             return "INTEGER"
        case float():           return "REAL"
        case str():             return "TEXT"
        case datetime():        return "TIMESTAMP"
        case date():            return "DATE"
        case bytes():           return "BLOB"
        case list() | tuple() | dict(): return "JSON"
        case Decimal():         return "DECIMAL"
        case uuid.UUID():       return "UUID"
        case _:                 return ""


number_types = {"REAL", "INTEGER"}

# Map Python built-in types to SQLite declared types.
_PYTHON_TYPE_MAP: dict[type, str] = {
    bool:     "BOOLEAN",
    int:      "INTEGER",
    float:    "REAL",
    str:      "TEXT",
    bytes:    "BLOB",
    list:     "JSON",
    tuple:    "JSON",
    dict:     "JSON",
    datetime: "TIMESTAMP",
    date:     "DATE",
    Decimal:  "DECIMAL",
    uuid.UUID:"UUID",
}

def normalize_field_types(fields: dict) -> dict:
    """
    Normalise a field-spec dict so every value is an uppercase SQLite type string.

    Accepts:
      - Python types:      {'age': int, 'name': str}
      - SQLite strings:    {'age': 'INTEGER', 'name': 'TEXT'}
      - Mixed:             {'age': int, 'note': 'TEXT'}
    """
    result = {}
    for col, spec in fields.items():
        if isinstance(spec, type):
            sql_type = _PYTHON_TYPE_MAP.get(spec)
            if sql_type is None:
                raise TypeError(
                    f"Unsupported Python type {spec!r} for column '{col}'. "
                    f"Supported: {list(_PYTHON_TYPE_MAP.keys())}"
                )
            result[col] = sql_type
        elif isinstance(spec, str):
            result[col] = spec.upper()
        else:
            raise TypeError(
                f"Column spec for '{col}' must be a Python type or SQLite type string, "
                f"got {type(spec)!r}"
            )
    return result


def _adapt_value(value: Any) -> Any:
    """Convert a Python value to an sqlite3-safe type for parameter binding."""
    if value is None:                           return None
    if isinstance(value, bool):                 return int(value)
    if isinstance(value, (list, tuple, dict)):  return json.dumps(value, default=str)
    if isinstance(value, Decimal):              return str(value)
    if isinstance(value, uuid.UUID):            return str(value)
    if isinstance(value, datetime):             return value.isoformat()
    if isinstance(value, date):                 return value.isoformat()
    return value


def _convert_value(value: Any, declared_type: str) -> Any:
    """
    Convert a raw sqlite3 value back to the appropriate Python type.

    Guards against double-conversion: when PARSE_DECLTYPES is active,
    registered converters may already have run, so we check isinstance
    before attempting a second conversion.

    All conversions are wrapped in try/except so a corrupt or NULL cell
    never crashes an entire SELECT — the raw value is returned instead.
    """
    if value is None:
        return None
    t = declared_type.upper()
    try:
        if t == "JSON":
            return json.loads(value) if isinstance(value, (str, bytes)) else value
        if t == "BOOLEAN":
            return value if isinstance(value, bool) else bool(int(value))
        if t == "DECIMAL":
            return value if isinstance(value, Decimal) else Decimal(value)
        if t == "UUID":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if t == "DATE":
            if isinstance(value, datetime): return value.date()
            if isinstance(value, date):     return value
            return date.fromisoformat(value)
        if t == "TIMESTAMP":
            if isinstance(value, datetime): return value
            return datetime.fromisoformat(value)
    except (ValueError, TypeError, AttributeError):
        return value
    return value


def _equal_field_dicts(d1: dict, d2: dict) -> bool:
    return d1.keys() == d2.keys() and all(
        d1[k].upper() == d2[k].upper() for k in d1
    )


# ── sqlite3 adapters / converters — registered once at import time ────────────

sqlite3.register_adapter(bool,       int)
sqlite3.register_adapter(list,       lambda v: json.dumps(v, default=str))
sqlite3.register_adapter(tuple,      lambda v: json.dumps(list(v), default=str))
sqlite3.register_adapter(dict,       lambda v: json.dumps(v, default=str))
sqlite3.register_adapter(Decimal,    str)
sqlite3.register_adapter(uuid.UUID,  str)

sqlite3.register_converter("JSON",      lambda v: json.loads(v))
sqlite3.register_converter("BOOLEAN",   lambda v: bool(int(v)))
sqlite3.register_converter("DECIMAL",   lambda v: Decimal(v.decode()))
sqlite3.register_converter("UUID",      lambda v: uuid.UUID(v.decode()))

def _conv_date(v):
    try:
        return date.fromisoformat(v.decode()) if v else None
    except (ValueError, AttributeError):
        return v.decode() if isinstance(v, bytes) else v

def _conv_timestamp(v):
    try:
        return datetime.fromisoformat(v.decode()) if v else None
    except (ValueError, AttributeError):
        return v.decode() if isinstance(v, bytes) else v

sqlite3.register_converter("DATE",      _conv_date)
sqlite3.register_converter("TIMESTAMP", _conv_timestamp)


# ── Smart Schema Evolution ────────────────────────────────────────────────────

# Type compatibility groups: any type within a group can be safely cast to another.
_TYPE_COMPAT = {
    "INTEGER": "numeric", "REAL": "numeric", "BOOLEAN": "numeric",
    "TEXT": "text", "JSON": "text", "UUID": "text", "DECIMAL": "text",
    "DATE": "text", "TIMESTAMP": "text",
}


def _types_compatible(t1: str, t2: str) -> bool:
    """Return True if two declared SQLite column types are safely inter-convertible."""
    g1 = _TYPE_COMPAT.get(t1.upper(), t1.upper())
    g2 = _TYPE_COMPAT.get(t2.upper(), t2.upper())
    return g1 == g2


def interactive_migration_choice(
    table_id: str, old_fields: dict, new_fields: dict
) -> tuple | None:
    """
    Analyse a schema change and interactively ask the user how to proceed.

    Returns:
        None                     – user cancelled
        ("recreate", {})         – drop and rebuild (data lost)
        ("exact",   {new→old})   – migrate exact matches only
        ("max",     {new→old})   – migrate exact + fuzzy matches
    """
    old_keys_lower = {k.lower(): k for k in old_fields}
    new_keys_lower = {k.lower(): k for k in new_fields}

    # ── exact matches (case-insensitive) ──────────────────────────────────
    exact_matches: dict[str, str] = {}
    for nk_lower, nk in new_keys_lower.items():
        if nk_lower in old_keys_lower:
            ok = old_keys_lower[nk_lower]
            exact_matches[nk] = ok

    # ── fuzzy matches (Levenshtein heuristic, cutoff 0.6) ─────────────────
    fuzzy_matches: dict[str, str] = {}
    remaining_old = {k for k in old_fields if k not in exact_matches.values()}
    remaining_new = {k for k in new_fields if k not in exact_matches}

    for nk in remaining_new:
        candidates = difflib.get_close_matches(nk, remaining_old, n=1, cutoff=0.6)
        if candidates:
            ok = candidates[0]
            if _types_compatible(new_fields[nk], old_fields[ok]):
                fuzzy_matches[nk] = ok
                remaining_old.discard(ok)

    # ── console prompt ────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print(f"  SCHEMA CHANGE DETECTED  —  table [{table_id}]")
    print("=" * 64)
    print(f"  Old fields : {list(old_fields.keys())}")
    print(f"  New fields : {list(new_fields.keys())}")
    if exact_matches:
        print(f"  Exact matches  : {exact_matches}")
    if fuzzy_matches:
        print(f"  Fuzzy matches  : {fuzzy_matches}")
    print()
    print("  [1] Cancel (abort, keep old table untouched)")
    print("  [2] Recreate table (DROP old data, build fresh)")
    print("  [3] Migrate exact matches only")
    print("  [4] Maximum migration (exact + similar fields)")
    print()

    try:
        choice = input("  Your choice [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if choice == "1":
        print("  → Cancelled. Keeping old table as-is.")
        return None
    elif choice == "2":
        print("  → Recreating table (old data will be lost).")
        return ("recreate", {})
    elif choice == "3":
        print(f"  → Migrating exact matches: {exact_matches}")
        return ("exact", exact_matches)
    elif choice == "4":
        combined = {**exact_matches, **fuzzy_matches}
        print(f"  → Maximum migration: {combined}")
        return ("max", combined)
    else:
        print("  → Unknown option. Cancelling.")
        return None


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """
    SQLite backend — the single, built-in database engine for UNISI.

    Features: WAL journal mode, native SQL-injection protection via
    parameterised queries, ON DELETE CASCADE for junction tables, and
    Smart Schema Evolution with interactive migration prompts.
    """

    def __init__(self, dbpath: str, message_logger=print) -> None:
        self.tables: dict[str, "Dbtable"] = {}
        self.dbpath = dbpath
        self.message_logger = message_logger

        os.makedirs(os.path.dirname(os.path.abspath(dbpath)), exist_ok=True)

        # PARSE_DECLTYPES activates the registered converters above.
        self._conn = sqlite3.connect(
            dbpath,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

        import inspect
        sig = inspect.signature(self.get_table)
        self.table_params = {
            k: v.default
            for k, v in sig.parameters.items()
            if v.default is not inspect.Parameter.empty
        }

    # ── low-level execution ──────────────────────────────────────────────── #

    def execute(
        self, query: str, params=(), ignore_exception: bool = False
    ) -> sqlite3.Cursor | None:
        try:
            cur = self._conn.cursor()
            cur.execute(query, params)
            self._conn.commit()
            return cur
        except sqlite3.Error as e:
            if not ignore_exception:
                self.message_logger(f"SQL Error: {e}\nQuery: {query}")
            return None

    def executemany(
        self, query: str, params_seq, ignore_exception: bool = False
    ) -> sqlite3.Cursor | None:
        try:
            cur = self._conn.cursor()
            cur.executemany(query, params_seq)
            self._conn.commit()
            return cur
        except sqlite3.Error as e:
            if not ignore_exception:
                self.message_logger(f"SQL Error (executemany): {e}\nQuery: {query}")
            return None

    @staticmethod
    def delete(dir_path: str) -> None:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path) if os.path.isdir(dir_path) else os.remove(dir_path)

    def close(self):
        self._conn.close()

    # ── schema ───────────────────────────────────────────────────────────── #

    @property
    def table_names(self) -> list[str]:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row[0] for row in cur.fetchall()]

    def get_table_fields(
        self, table_name: str, remove_id: bool = True
    ) -> dict | None:
        cur = self._conn.execute(f"PRAGMA table_info('{table_name}')")
        rows = cur.fetchall()
        if not rows:
            return None
        return {
            row["name"]: row["type"]
            for row in rows
            if not remove_id or row["name"] != "ID"
        }

    def delete_table(self, table_name: str) -> bool:
        return self.execute(f"DROP TABLE IF EXISTS [{table_name}]") is not None

    # ── table factory ────────────────────────────────────────────────────── #

    def _find_backup_name(self, table_id: str) -> str:
        """Return a free backup table name like {table_id}_OLD_1, _OLD_2, …"""
        existing = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        n = 1
        while True:
            name = f"{table_id}_OLD_{n}"
            if name not in existing:
                return name
            n += 1

    def _migrate_table(
        self,
        table_id: str,
        old_fields: dict,
        new_fields: dict,
        limit: int,
        rows,
    ) -> "Dbtable | None":
        """
        Perform an interactive schema migration: backup → recreate → copy data.

        Called when get_table() detects a field mismatch.
        The old table is always preserved as {table_id}_OLD_N for safety —
        it is never dropped automatically.

        Returns the new Dbtable on success, or None if the user cancelled.
        """
        result = interactive_migration_choice(table_id, old_fields, new_fields)
        if result is None:
            # User cancelled — return a Dbtable wrapping the old (unchanged) table.
            return self.tables.get(table_id) or Dbtable(table_id, self, limit, old_fields)

        action, mapping = result

        if action == "recreate":
            # Drop old table entirely and create fresh (data is lost).
            self.delete_table(table_id)
            return self.create_table(table_id, new_fields, limit, rows)

        # ── exact / max migration ─────────────────────────────────────────
        backup_name = self._find_backup_name(table_id)
        try:
            self._conn.execute("PRAGMA foreign_keys=OFF")

            # 1. Rename old table to backup (old data is preserved here).
            self._conn.execute(
                f"ALTER TABLE [{table_id}] RENAME TO [{backup_name}]"
            )
            self._conn.commit()

            # 2. Create the new table with the updated schema.
            new_table = self.create_table(table_id, new_fields, limit)

            # 3. Build the column mapping for data transfer.
            new_cols = list(mapping.keys())
            old_cols = [mapping[nc] for nc in new_cols]
            new_cols_str = ", ".join(f"[{c}]" for c in new_cols)
            old_cols_str = ", ".join(f"[{c}]" for c in old_cols)

            # 4. Copy data (including the ID column to preserve relationships).
            self._conn.execute(
                f"INSERT INTO [{table_id}] ({new_cols_str}, [ID]) "
                f"SELECT {old_cols_str}, [ID] FROM [{backup_name}]"
            )
            self._conn.commit()

            # 5. Refresh list to reflect migrated data.
            new_table.init_list()

            # Backup table is intentionally kept — never dropped.
            self.message_logger(
                f"Table '{table_id}' migrated successfully ({len(new_cols)} "
                f"columns transferred, ID preserved). "
                f"Backup kept as '{backup_name}'.",
                "info",
            )
            return new_table

        except Exception as e:
            self.message_logger(f"Migration error for '{table_id}': {e}")
            return None
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.commit()

    def get_table(
        self,
        id: str = None,
        limit: int = 100,
        headers: list = None,
        rows: list = None,
        fields: dict = None,
    ) -> "Dbtable | None":
        if not id:
            return None

        if fields is not None:
            fields = normalize_field_types(fields)

        if rows and fields is None:
            if not headers:
                self.message_logger("headers are not defined!")
                return None
            types = [None] * len(headers)
            for row in rows:
                for j, cell in enumerate(row):
                    if cell is not None:
                        stype = sqlite_data_type(cell)
                        if stype:
                            if types[j] is None:
                                types[j] = stype
                            elif types[j] != stype:
                                if types[j] in number_types and stype in number_types:
                                    types[j] = "REAL"
                                else:
                                    self.message_logger(
                                        f"Conflicting types for '{id}' column "
                                        f"{j}: {types[j]} vs {stype}",
                                        "warning",
                                    )
                                    return None
            if None in types:
                idx = types.index(None)
                self.message_logger(
                    f"Cannot infer type for column '{headers[idx]}'"
                )
                return None
            fields = {headers[i]: t for i, t in enumerate(types)}

        existing_fields = self.get_table_fields(id)
        if existing_fields is not None:
            if fields is not None and not _equal_field_dicts(existing_fields, fields):
                # Schema mismatch — invoke Smart Schema Evolution.
                return self._migrate_table(id, existing_fields, fields, limit, rows)
            else:
                return self.tables.get(id) or Dbtable(id, self, limit, existing_fields)

        return self.create_table(id, fields, limit, rows)

    def get_table_params(self, params: dict) -> dict:
        return {k: v for k, v in params.items() if k in self.table_params}

    def set_db_list(self, gui_table):
        table = self.get_table(**self.get_table_params(gui_table.__dict__))
        gui_table.rows = table.list

    def create_table(
        self, id: str, fields: dict, limit: int = 100, rows=None
    ) -> "Dbtable":
        cols = ", ".join(f"[{col}] {type_}" for col, type_ in fields.items())
        self.execute(
            f"CREATE TABLE IF NOT EXISTS [{id}] "
            f"({cols}, ID INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        table = Dbtable(id, self, limit, fields)
        if rows:
            table.list.extend(rows)
        return table

    # ── row-level update ─────────────────────────────────────────────────── #

    def update_row(
        self,
        table_id: str,
        row_id: int,
        props: dict,
        in_node: bool = True,
    ) -> bool:
        set_clause = ", ".join(f"[{k}] = ?" for k in props)
        params = [_adapt_value(v) for v in props.values()] + [row_id]
        return self.execute(
            f"UPDATE [{table_id}] SET {set_clause} WHERE ID = ?", params
        ) is not None

    # ── query helpers ────────────────────────────────────────────────────── #

    def qlist(
        self,
        query: str,
        params=(),
        func=None,
        ignore_exception: bool = False,
    ) -> list | None:
        cur = self.execute(query, params, ignore_exception)
        if cur is None:
            return None
        return [func(r) if func else list(r) for r in cur.fetchall()]

    def qiter(
        self, query: str, params=(), func=None, ignore_exception: bool = False
    ):
        cur = self.execute(query, params, ignore_exception)
        if cur:
            for row in cur:
                yield func(row) if func else list(row)


# ── Dbtable ───────────────────────────────────────────────────────────────────

class Dbtable:
    """
    Wraps a single SQLite table.

    Column order: [user_fields …, ID]
    Every row returned as a plain Python list in that order.
    """

    def __init__(
        self,
        id: str,
        db: Database,
        limit: int = 100,
        table_fields: dict = None,
    ) -> None:
        self.db = db
        db.tables[id] = self
        self.id = id
        self.limit = limit
        self.table_fields: dict = table_fields or db.get_table_fields(id) or {}
        self.node_columns: list[str] = list(self.table_fields.keys())
        self._all_columns: list[str] = self.node_columns + ["ID"]
        self.init_list()

    # ── internal helpers ─────────────────────────────────────────────────── #

    def _select_cols(self) -> str:
        """Unqualified column list for simple SELECT … FROM [{id}]."""
        return ", ".join(f"[{c}]" for c in self._all_columns)

    def _aliased_select_cols(self, alias: str = "a") -> str:
        """Alias-qualified column list for JOINs to avoid ambiguous 'ID'."""
        return ", ".join(f"{alias}.[{c}]" for c in self._all_columns)

    def _row_to_list(self, row) -> list:
        """Convert a sqlite3.Row to a typed Python list, applying converters."""
        result = []
        for i, key in enumerate(self._all_columns):
            val = row[i] if not isinstance(row, sqlite3.Row) else row[key]
            dtype = self.table_fields.get(key, "")
            result.append(_convert_value(val, dtype))
        return result

    # ── list initialisation ──────────────────────────────────────────────── #

    def init_list(self):
        rows = self.read_rows(limit=self.limit)
        length = len(rows)
        if length == self.limit:
            cnt = self.db.qlist(f"SELECT COUNT(*) FROM [{self.id}]")
            self.length = cnt[0][0] if cnt else 0
        else:
            self.length = length
        self.list = Dblist(self, rows)

    # ── read ─────────────────────────────────────────────────────────────── #

    def read_rows(self, skip: int = 0, limit: int = 0) -> list[list]:
        lim = limit if limit else self.limit
        cur = self.db.execute(
            f"SELECT {self._select_cols()} FROM [{self.id}] "
            f"ORDER BY ID LIMIT ? OFFSET ?",
            (lim, skip),
        )
        return [self._row_to_list(r) for r in cur.fetchall()] if cur else []

    # ── write ────────────────────────────────────────────────────────────── #

    def assign_row(self, row_array: list) -> bool:
        """Update a DB row from a list.  Last element must be the row's DB ID."""
        row_id = row_array[-1]
        props = {name: row_array[i] for i, name in enumerate(self.node_columns)}
        return self.db.update_row(self.id, row_id, props)

    def append_row(self, row) -> list | None:
        """Insert a single row (list or dict); return the stored row with ID."""
        if isinstance(row, list):
            props = {
                name: row[i]
                for i, name in enumerate(self.node_columns)
                if i < len(row) and row[i] is not None
            }
        elif isinstance(row, dict):
            props = {k: v for k, v in row.items() if v is not None}
        else:
            raise TypeError(f"row must be list or dict, got {type(row).__name__}")

        cols         = ", ".join(f"[{k}]" for k in props)
        placeholders = ", ".join("?" for _ in props)
        values       = [_adapt_value(v) for v in props.values()]

        cur = self.db.execute(
            f"INSERT INTO [{self.id}] ({cols}) VALUES ({placeholders})", values
        )
        if cur is None:
            return None
        new_id = cur.lastrowid
        self.length += 1

        read_cur = self.db.execute(
            f"SELECT {self._select_cols()} FROM [{self.id}] WHERE ID = ?", (new_id,)
        )
        raw = read_cur.fetchone() if read_cur else None
        return self._row_to_list(raw) if raw else None

    def append_rows(self, rows: list) -> list[list]:
        """
        Bulk-insert rows atomically and return each stored row with its ID.

        Uses RETURNING * via ``execute()`` in a single explicit transaction.
        This is race-condition-free: each row carries its own ID back from the
        DB immediately, with no gap for a concurrent writer.

        Note: Python's sqlite3 C binding does not support RETURNING with
        ``executemany()``, so we loop over ``execute()`` calls instead.
        The explicit ``BEGIN`` / ``COMMIT`` keeps the whole batch atomic and
        avoids the per-row auto-commit overhead.
        """
        if not rows:
            return []

        dicts: list[dict] = []
        for row in rows:
            if isinstance(row, list):
                dicts.append({
                    name: row[i]
                    for i, name in enumerate(self.node_columns)
                    if i < len(row)
                })
            elif isinstance(row, dict):
                dicts.append(row)
            else:
                raise TypeError(f"Unsupported row type: {type(row)}")

        cols         = list(dicts[0].keys())
        col_str      = ", ".join(f"[{c}]" for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        sql          = (
            f"INSERT INTO [{self.id}] ({col_str}) "
            f"VALUES ({placeholders}) RETURNING *"
        )

        inserted: list[list] = []
        try:
            # Use the connection as a context manager rather than explicit
            # BEGIN / COMMIT.  Explicit BEGIN raises
            # "cannot start a transaction within a transaction" if Python's
            # sqlite3 module has already opened an implicit transaction (which
            # happens whenever a DML statement ran without an intervening
            # commit).  The context manager detects this correctly: it joins
            # an existing transaction if one is open, or starts a new one, and
            # always issues COMMIT on clean exit or ROLLBACK on any exception.
            with self.db._conn:
                for d in dicts:
                    params = tuple(_adapt_value(d.get(c)) for c in cols)
                    cur = self.db._conn.execute(sql, params)
                    raw = cur.fetchone()
                    if raw is not None:
                        inserted.append(self._row_to_list(raw))
        except sqlite3.Error as e:
            self.db.message_logger(f"append_rows failed: {e}")
            return []

        self.length += len(inserted)
        return inserted

    def delete_row(self, row_id: int) -> bool:
        """
        Delete a row by its DB primary key.

        Receives the actual ID (extracted from row[-1] by Dblist.__delitem__),
        never a list offset.  This is correct even when the ID sequence has
        gaps from prior deletions.
        """
        self.length -= 1
        result = self.db.execute(
            f"DELETE FROM [{self.id}] WHERE ID = ?", (row_id,)
        )
        return result is not None

    def delete_rows(self, ids) -> bool:
        ids = list(ids)
        ph  = ", ".join("?" for _ in ids)
        result = self.db.execute(
            f"DELETE FROM [{self.id}] WHERE ID IN ({ph})", ids
        )
        if result is not None:
            self.length -= result.rowcount
        return result is not None

    def clear(self, detach: bool = False) -> bool:
        """
        Delete all rows.

        *detach* is accepted for API compatibility; in SQLite the equivalent
        is handled automatically by ON DELETE CASCADE on junction tables.
        """
        result = self.db.execute(f"DELETE FROM [{self.id}]")
        if result is not None:
            self.length = 0
        return result is not None

    # ── relation helpers (junction tables) ───────────────────────────────── #

    def default_index_name2(self, link_table: str) -> str:
        return f"{self.id}2{link_table}"

    def _existing_tables(self) -> set[str]:
        cur = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        return {row[0] for row in cur.fetchall()}

    def get_rel_fields2(
        self,
        tname: str,
        fields: dict = None,
        relname: str = None,
    ) -> tuple[str, dict]:
        """
        Return (rel_table_name, fields_dict), creating the junction table if needed.

        For SQLite the simplest relation is a plain junction table with just
        src_id / tgt_id (no extra payload columns).  Extra fields (formerly
        Kuzu edge properties) are still supported when *fields* is non-empty.

        The default junction-table name is ``{self.id}2{tname}``.
        """
        if not relname:
            relname = self.default_index_name2(tname)

        if fields is not None:
            fields = normalize_field_types(fields)

        existing = self.db.get_table_fields(relname)
        if existing is not None:
            # Junction table already exists.
            if isinstance(fields, dict) and fields:
                # Caller specifies extra fields — check compatibility.
                if _equal_field_dicts(existing, fields):
                    return relname, existing
                else:
                    # Schema changed — drop and recreate.
                    self.db.delete_table(relname)
            else:
                # No extra fields requested (simple link = utable case).
                # Accept whatever is already there.
                return relname, existing if existing else {}

        # Table does not exist (or was just dropped) — create it.
        if fields is None:
            fields = {}

        extra = (
            ", " + ", ".join(f"[{k}] {v}" for k, v in fields.items())
            if fields else ""
        )
        self.db.execute(
            f"CREATE TABLE [{relname}] ("
            f"src_id INTEGER REFERENCES [{self.id}](ID) ON DELETE CASCADE, "
            f"tgt_id INTEGER REFERENCES [{tname}](ID)  ON DELETE CASCADE"
            f"{extra}, "
            f"ID INTEGER PRIMARY KEY AUTOINCREMENT"
            f")"
        )
        return relname, fields

    def add_link(
        self,
        snode_id: int,
        link_table: str,
        tnode_id: int,
        link_fields: dict = None,
        link_index_name: str = None,
    ) -> list | None:
        if link_index_name is None:
            link_index_name = self.default_index_name2(link_table)
        if link_fields is None:
            link_fields = {}

        all_fields   = {"src_id": snode_id, "tgt_id": tnode_id, **link_fields}
        cols         = ", ".join(f"[{k}]" for k in all_fields)
        placeholders = ", ".join("?" for _ in all_fields)
        values       = [_adapt_value(v) for v in all_fields.values()]

        cur = self.db.execute(
            f"INSERT INTO [{link_index_name}] ({cols}) VALUES ({placeholders})",
            values,
        )
        if cur is None:
            return None
        new_id   = cur.lastrowid
        read_cur = self.db.execute(
            f"SELECT * FROM [{link_index_name}] WHERE ID = ?", (new_id,)
        )
        raw = read_cur.fetchone() if read_cur else None
        return list(raw) if raw else None

    def add_links(
        self,
        link_table: str,
        snode_ids,
        tnode_id: int,
        link_index_name: str = None,
    ) -> list:
        return [
            self.add_link(
                sid, link_table, tnode_id, link_index_name=link_index_name
            )
            for sid in snode_ids
        ]

    def delete_link(
        self, link_table_id: str, link_id: int, index_name: str = None
    ) -> bool:
        if not index_name:
            index_name = self.default_index_name2(link_table_id)
        return self.db.execute(
            f"DELETE FROM [{index_name}] WHERE ID = ?", (link_id,)
        ) is not None

    def delete_links(
        self,
        link_table_id: str,
        link_node_id: int = None,
        source_ids=None,
        link_ids=None,
        index_name: str = None,
    ) -> bool:
        if not index_name:
            index_name = self.default_index_name2(link_table_id)

        if link_ids:
            ids = list(link_ids)
            ph  = ", ".join("?" for _ in ids)
            result = self.db.execute(
                f"DELETE FROM [{index_name}] WHERE ID IN ({ph})", ids
            )
        else:
            if not isinstance(source_ids, list):
                source_ids = list(source_ids)
            ph = ", ".join("?" for _ in source_ids)
            result = self.db.execute(
                f"DELETE FROM [{index_name}] WHERE src_id IN ({ph}) AND tgt_id = ?",
                source_ids + [link_node_id],
            )
        return result is not None

    def calc_linked_rows(
        self,
        index_name: str,
        link_ids,
        target_table: str,
        include_rels: bool = False,
        search: str = "",
    ) -> Dblist:
        """Return a Dblist of source rows linked to any row in *link_ids*."""
        ids = list(link_ids)
        ph  = ", ".join("?" for _ in ids)
        rel_cols = ", r.*" if include_rels else ""
        query = (
            f"SELECT {self._aliased_select_cols('a')}{rel_cols} "
            f"FROM [{self.id}] a "
            f"JOIN [{index_name}] r ON r.src_id = a.[ID] "
            f"JOIN [{target_table}] b ON r.tgt_id = b.[ID] "
            f"WHERE b.[ID] IN ({ph}) "
            f"ORDER BY a.[ID] ASC"
        )
        cur = self.db.execute(query, ids)
        lst = [self._row_to_list(r) for r in cur.fetchall()] if cur else []
        return Dblist(self, cache=lst)


# ── Auto-initialisation ───────────────────────────────────────────────────────

def _init_db() -> "Database | None":
    """
    Resolve the database path and return an initialised Database instance.

    Priority order:
      1. ``config.db_path`` (from the user's config module)
      2. ``UNISI_DB_PATH`` environment variable
      3. None (database disabled)
    """
    db_path: str | None = None

    # 1. Try the user's config module (may not exist for all projects).
    try:
        import config as _config
        db_path = getattr(_config, "db_path", None)
    except ImportError:
        pass

    # 2. Fall back to environment variable.
    if not db_path:
        db_path = os.environ.get("UNISI_DB_PATH")

    if not db_path:
        return None

    # If the path resolves to an existing directory (e.g. the old Kuzu
    # db_dir pointed at a folder), treat it as the database *directory*
    # and create a SQLite file inside it.
    abs_path = os.path.abspath(db_path)
    if os.path.isdir(abs_path):
        db_path = os.path.join(abs_path, "unisi.db")

    # Lazy import avoids circular dependency: common.py is imported by
    # other unisi modules, and we don't want common → db → common cycles
    # at module-load time.
    from .common import Unishare

    def _logger(message, type="error"):
        if callable(Unishare.message_logger):
            Unishare.message_logger(message, type)
        else:
            print(f"[{type}] {message}")

    return Database(db_path, message_logger=_logger)


db: Database | None = _init_db()
