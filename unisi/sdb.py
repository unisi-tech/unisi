# Copyright © 2024 UNISI Tech. All rights reserved.
"""
sdb.py — SQLite backend for the UNISI db layer.

Public API mirrors kdb.py (Database + Dbtable) so Dblist and all
higher-level code work with both backends without modification.

Extended type support vs. Kuzu
──────────────────────────────
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
its outgoing and incoming links — equivalent to Kuzu's DETACH DELETE.

Variable-count IN clauses
─────────────────────────
SQLite limits bind variables per statement to SQLITE_MAX_VARIABLE_NUMBER
(32 766 in Python's bundled SQLite ≥ 3.32, 999 in older builds).
calc_linked_rows / delete_links accept arbitrary iterables of IDs; callers
with very large sets (> ~1000 on older SQLite) should batch externally.
"""

import sqlite3
import json
import os
import shutil
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
        # Return the raw value rather than crashing the whole row fetch.
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


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """
    SQLite backend.  Public API is identical to kdb.Database.
    """

    def __init__(self, dbpath: str, message_logger=print) -> None:
        # Per-instance table registry (mirrors the fix applied to kdb.py).
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
        # ON DELETE CASCADE in junction tables requires FK enforcement.
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
                if self.delete_table(id):
                    self.message_logger(
                        f"Table '{id}' dropped due to schema mismatch.", "warning"
                    )
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
        in_node: bool = True,   # ignored for SQLite (no separate rel tables)
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
    Wraps a single SQLite table.  API mirrors kdb.Dbtable.

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
            self.db._conn.execute("BEGIN")
            for d in dicts:
                params = tuple(_adapt_value(d.get(c)) for c in cols)
                cur = self.db._conn.execute(sql, params)
                raw = cur.fetchone()
                if raw is not None:
                    inserted.append(self._row_to_list(raw))
            self.db._conn.execute("COMMIT")
        except sqlite3.Error as e:
            self.db._conn.execute("ROLLBACK")
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

        *detach* is accepted for API parity with kdb; in SQLite the equivalent
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
        """Return (rel_table_name, fields_dict), creating the junction table if needed."""
        if not relname:
            relname = self.default_index_name2(tname)

        existing = self.db.get_table_fields(relname)
        if existing is not None:
            if isinstance(fields, dict):
                if _equal_field_dicts(existing, fields):
                    return relname, existing
                else:
                    self.db.delete_table(relname)
            else:
                fields = existing
        elif fields is None:
            fields = {}

        if relname not in self._existing_tables():
            extra = (
                ", " + ", ".join(f"[{k}] {v}" for k, v in fields.items())
                if fields else ""
            )
            # ON DELETE CASCADE: deleting a source or target node automatically
            # removes its junction rows — equivalent to Kuzu DETACH DELETE.
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
