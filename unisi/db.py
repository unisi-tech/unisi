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
  [float,float]  │ POINT                │ two REAL columns, {name}_x/{name}_y
  (float,float)  │                      │ (x=longitude, y=latitude); see §11
                 │                      │ "Geo-Spatial Fields" in the docs

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

import dataclasses
import difflib
import json
import math
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

# ── Geo-spatial (POINT) fields ──────────────────────────────────────────────
#
# A field declared as a 2-element list/tuple of `float`/`int` *types* --
# {'position': [float, float]}  or  {'position': (float, float)} -- is
# auto-detected as a geo-spatial point and normalised to the logical type
# "POINT". This is deliberately distinct from the pre-existing {'tags': list}
# / {'tags': tuple} spec (a plain Python *type*, not a 2-element container of
# types), which keeps mapping to a single JSON column as before.
#
# Storage: two physical REAL columns per point field, `{name}_x` / `{name}_y`
# (GIS/PostGIS convention: x = longitude/easting, y = latitude/northing --
# i.e. Point(x, y) == Point(lon, lat)), plus a composite index on (y, x) so
# the bounding-box pre-filter in search_within_radius()/search_nearest() can
# use a B-Tree range scan instead of a full table scan. The two sub-columns
# are declared with the distinct type names REAL_X / REAL_Y (still REAL
# affinity -- see _physical_field_columns) purely so that
# Database.get_table_fields() can unambiguously fold them back into one
# logical "POINT" field when re-reading the schema from PRAGMA table_info;
# see _fold_point_columns().
#
# Everywhere else in this module, "logical" fields/columns means one entry
# per declared field (what the user wrote, what Dbtable.table_fields/
# node_columns expose, what a GUI row has one cell for), and "physical"
# means actual SQLite columns (where a POINT field is two). Dbtable computes
# the logical <-> physical mapping once in self.point_fields and every
# SQL-building / row-conversion method below is written in terms of it.
GEO_TYPE = "POINT"
_POINT_X_TYPE = "REAL_X"
_POINT_Y_TYPE = "REAL_Y"


def _is_point_spec(spec: Any) -> bool:
    """True for a 2-element list/tuple of float/int *types*, e.g. [float, float]."""
    return (
        isinstance(spec, (list, tuple))
        and len(spec) == 2
        and all(s in (float, int) for s in spec)
    )


def _point_columns(field: str) -> tuple[str, str]:
    """Physical (x_column, y_column) names backing a logical POINT field."""
    return f"{field}_x", f"{field}_y"


def normalize_field_types(fields: dict) -> dict:
    """
    Normalise a field-spec dict so every value is an uppercase SQLite type string.

    Accepts:
      - Python types:      {'age': int, 'name': str}
      - SQLite strings:    {'age': 'INTEGER', 'name': 'TEXT'}
      - Mixed:             {'age': int, 'note': 'TEXT'}
      - Geo-spatial point:  {'position': [float, float]} or (float, float)
        -> logical type "POINT" (see the block comment above)
    """
    result = {}
    for col, spec in fields.items():
        if _is_point_spec(spec):
            result[col] = GEO_TYPE
        elif isinstance(spec, type):
            sql_type = _PYTHON_TYPE_MAP.get(spec)
            if sql_type is None:
                raise TypeError(
                    f"Unsupported Python type {spec!r} for column '{col}'. "
                    f"Supported: {list(_PYTHON_TYPE_MAP.keys())}"
                )
            result[col] = sql_type
        elif isinstance(spec, str):
            result[col] = spec.upper()
        elif isinstance(spec, (list, tuple)):
            raise TypeError(
                f"Column spec for '{col}' is a {type(spec).__name__} but isn't a "
                f"2-element [float, float] / (float, float) geo-point spec, got {spec!r}. "
                f"Only that shape is auto-detected as a POINT field."
            )
        else:
            raise TypeError(
                f"Column spec for '{col}' must be a Python type or SQLite type string, "
                f"got {type(spec)!r}"
            )
    return result


def _physical_field_columns(fields: dict) -> list[tuple[str, str]]:
    """
    Expand a *logical* fields dict (POINT included) into the flat list of
    *physical* (column_name, declared_sql_type) pairs that actually exist in
    SQLite -- each POINT field becomes two REAL_X/REAL_Y sub-columns, in x,
    then y order. Used everywhere a CREATE TABLE / column list needs to
    reflect the real schema (create_table, setup_junction, schema-migration
    data copy).
    """
    cols: list[tuple[str, str]] = []
    for name, sql_type in fields.items():
        if sql_type.upper() == GEO_TYPE:
            x_col, y_col = _point_columns(name)
            cols.append((x_col, _POINT_X_TYPE))
            cols.append((y_col, _POINT_Y_TYPE))
        else:
            cols.append((name, sql_type))
    return cols


def _physical_column_names(names: list, fields: dict) -> list[str]:
    """Like _physical_field_columns but for a plain list of logical column
    names (e.g. a schema-migration mapping), returning names only."""
    out: list[str] = []
    for name in names:
        if fields.get(name, "").upper() == GEO_TYPE:
            out.extend(_point_columns(name))
        else:
            out.append(name)
    return out


def _fold_point_columns(raw_columns: list[tuple[str, str]]) -> dict:
    """
    Inverse of _physical_field_columns(): given the raw (name, declared_type)
    pairs read from PRAGMA table_info, fold any {base}_x/REAL_X +
    {base}_y/REAL_Y pair back into a single logical {base: "POINT"} entry.
    Column order is preserved (folded at the position of whichever half is
    encountered first). Everything else passes through unchanged.

    Requiring *both* the REAL_X/REAL_Y declared type *and* the _x/_y name
    suffix (rather than either alone) keeps this from ever mis-folding an
    ordinary pair of REAL columns that simply happen to be named
    "..._x"/"..._y" -- REAL_X/REAL_Y are never produced by any other code
    path in this module.
    """
    types = dict(raw_columns)
    point_bases = {
        name[:-2]
        for name, t in raw_columns
        if t.upper() == _POINT_X_TYPE and name.endswith("_x")
        and types.get(name[:-2] + "_y", "").upper() == _POINT_Y_TYPE
    }
    result: dict = {}
    for name, sql_type in raw_columns:
        if name.endswith(("_x", "_y")) and name[:-2] in point_bases:
            base = name[:-2]
            if base not in result:
                result[base] = GEO_TYPE
        else:
            result[name] = sql_type
    return result


def _split_point_value(value: Any) -> tuple:
    """Validate and unpack a POINT field value into its (x, y) components."""
    if value is None:
        return None, None
    try:
        x, y = value
    except (TypeError, ValueError):
        raise ValueError(
            f"POINT field value must be a 2-element [x, y] list/tuple, got {value!r}"
        )
    return x, y


def _expand_point_props(props: dict, point_fields: dict) -> dict:
    """
    Split any POINT-valued entries of *props* (a logical {field: value} dict
    bound for INSERT/UPDATE) into their physical _x/_y keys, ready for direct
    use as SQL column names. *point_fields* is a Dbtable.point_fields-shaped
    {name: (x_col, y_col)} mapping. Non-point entries pass through unchanged.
    """
    if not point_fields:
        return props
    expanded = {}
    for k, v in props.items():
        if k in point_fields:
            x_col, y_col = point_fields[k]
            x, y = _split_point_value(v)
            expanded[x_col] = x
            expanded[y_col] = y
        else:
            expanded[k] = v
    return expanded


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


# ── Geo-spatial distance ─────────────────────────────────────────────────────
#
# "Способ 1 + Способ 2" combined: an exact great-circle (haversine) distance
# function registered directly into SQLite via create_function(), plus a
# cheap equirectangular bounding-box pre-filter (search_within_radius() /
# search_nearest() below) that lets SQLite use a plain B-Tree range scan on
# indexed REAL columns to narrow candidates down *before* haversine_km() ever
# runs on them. Chosen over the R*Tree virtual-table module (fast, but an
# optional SQLite compile-time feature -- not guaranteed on every build) and
# over SpatiaLite (needs the external mod_spatialite binary, which is rarely
# preinstalled) because it needs nothing beyond the Python 3.10+ standard
# library and is more than fast enough up to the tens-of-thousands-of-rows
# range a single SQLite file is meant for. See docs/persistent_tables.md §11
# for the full comparison and the R*Tree upgrade path for larger tables.

EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius
_KM_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_KM / 180
_MIN_COS_LAT = 0.01          # clamps the longitude box near the poles


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float | None:
    """
    Great-circle distance in kilometres between two WGS-84 points.

    Registered verbatim as the SQL function ``haversine_km(lat1, lng1, lat2,
    lng2)`` on every Database connection (see Database.__init__), so it can
    also be used directly from hand-written SQL, not just through
    search_within_radius()/search_nearest().
    """
    if None in (lat1, lng1, lat2, lng2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _equal_field_dicts(d1: dict, d2: dict) -> bool:
    """
    True if d1 (existing, on-disk schema) and d2 (freshly declared fields)
    describe the same table for Smart Schema Evolution purposes.

    d1 is allowed exactly one extra column beyond d2: "link_id" (see
    Dbtable.LINK_ID). A many-to-one Table(link=...) always ends up with
    this column, but it's added by Dbtable.setup_fk() *after* get_table()
    already ran and compared schemas (see Table.__init__ in tables.py,
    where setup_fk() is only reachable once self.rows -- and therefore the
    Dbtable this comparison is for -- already exists). So on every restart
    against an existing linked table, d1 legitimately has link_id and d2
    (the caller's plain `fields=` dict, which never mentions the FK column
    it didn't ask for by name) never will.

    Without this allowance, every single restart of a server with any
    Table(link=...) triggers an interactive "SCHEMA CHANGE DETECTED"
    migration prompt (input()) for that table, forever, even though
    nothing about its fields ever changed -- confirmed by running the same
    app twice in a row against its own freshly-created database. For a
    headless deployment (systemd/Docker/etc.) that's a process hang on
    every restart, not just a cosmetic false alarm. See
    tests/db_units/test_db.py::TestManyToOneSchemaStability for the
    restart-simulation regression test, mirroring
    TestGeoSchemaStability's existing pattern for the analogous POINT-field
    case (_fold_point_columns) this fix is modelled on.
    """
    d1_keys = d1.keys()
    if Dbtable.LINK_ID not in d2.keys():
        # The caller didn't declare link_id explicitly -- ignore it as an
        # extra, framework-added column on the existing/on-disk side (see
        # the timing explanation above). If the caller DID declare it
        # themselves, compare it normally instead of silently ignoring a
        # real mismatch on it.
        d1_keys = d1_keys - {Dbtable.LINK_ID}
    if d1_keys != d2.keys():
        return False
    return all(d1[k].upper() == d2[k].upper() for k in d1_keys)


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

        # Powers search_within_radius()/search_nearest() and is usable from
        # raw SQL too. deterministic=True (SQLite >= 3.8.3, well within any
        # Python 3.10+ bundled version) lets the query planner treat repeat
        # calls with the same arguments as cacheable; the plain fallback
        # keeps this from being a hard requirement on unusual SQLite builds.
        try:
            self._conn.create_function(
                "haversine_km", 4, haversine_km, deterministic=True
            )
        except sqlite3.NotSupportedError:
            self._conn.create_function("haversine_km", 4, haversine_km)

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
        """
        Read the *logical* schema back from SQLite.

        Physical {name}_x/{name}_y REAL_X/REAL_Y pairs created for a POINT
        field (see _physical_field_columns) are folded back into one
        {name: "POINT"} entry via _fold_point_columns() -- this is what
        makes a POINT field declared as ``fields={'position': [float, float]}``
        compare equal to its own on-disk schema on every subsequent run
        (get_table() below), instead of Smart Schema Evolution firing on
        every restart because "position" (1 declared field) never matches
        "position_x, position_y" (2 raw columns).
        """
        cur = self._conn.execute(f"PRAGMA table_info('{table_name}')")
        rows = cur.fetchall()
        if not rows:
            return None
        raw = [
            (row["name"], row["type"])
            for row in rows
            if not remove_id or row["name"] != "ID"
        ]
        return _fold_point_columns(raw)

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

            # 3. Build the column mapping for data transfer. mapping is in
            # terms of *logical* names (e.g. a fuzzy rename "position" (new)
            # -> "location" (old)); a POINT entry on either side has to be
            # expanded to its physical _x/_y pair -- in matching x/y order on
            # both sides -- before it's usable as a raw SQL identifier.
            new_cols = list(mapping.keys())
            old_cols = [mapping[nc] for nc in new_cols]
            new_cols_phys = _physical_column_names(new_cols, new_fields)
            old_cols_phys = _physical_column_names(old_cols, old_fields)
            new_cols_str = ", ".join(f"[{c}]" for c in new_cols_phys)
            old_cols_str = ", ".join(f"[{c}]" for c in old_cols_phys)

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
        # get_table() and setup_junction() both normalise their `fields`
        # argument before use; create_table() is equally public (and the
        # module docstring advertises Python-type specs as a general
        # feature, not one scoped to get_table specifically), so it should
        # accept the same {'name': str} / {'name': 'TEXT'} / mixed forms
        # instead of interpolating a raw Python type into the CREATE TABLE
        # statement and failing with a confusing SQL syntax error.
        # normalize_field_types() is idempotent on already-normalised
        # (uppercase string) input, so this is a no-op when called via
        # get_table(), which normalises before delegating here.
        fields = normalize_field_types(fields)
        cols = ", ".join(
            f"[{col}] {type_}" for col, type_ in _physical_field_columns(fields)
        )
        self.execute(
            f"CREATE TABLE IF NOT EXISTS [{id}] "
            f"({cols}, ID INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        # One composite index per POINT field on (y, x) -- i.e. (lat, lng) --
        # so search_within_radius()/search_nearest()'s bounding-box
        # pre-filter can use a B-Tree range scan instead of a full scan.
        for name, sql_type in fields.items():
            if sql_type.upper() == GEO_TYPE:
                x_col, y_col = _point_columns(name)
                self.execute(
                    f"CREATE INDEX IF NOT EXISTS [{id}_{name}_geo_idx] "
                    f"ON [{id}] ([{y_col}], [{x_col}])"
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
        # A POINT-valued prop (e.g. {'position': [lng, lat]}) has to be split
        # into its physical position_x/position_y columns before it can be
        # used as a SQL identifier -- self.tables holds every table's
        # point_fields mapping (empty dict, hence a no-op, for tables with no
        # POINT fields or -- e.g. a junction row -- no Dbtable at all).
        dbtable = self.tables.get(table_id)
        props = _expand_point_props(props, dbtable.point_fields if dbtable else {})
        set_clause = ", ".join(f"[{k}] = ?" for k in props)
        params = [_adapt_value(v) for v in props.values()] + [row_id]
        ok = self.execute(
            f"UPDATE [{table_id}] SET {set_clause} WHERE ID = ?", params
        ) is not None
        if ok and dbtable:
            # Every Dblist.list read path (get_delta_chunk -> _sync_cache)
            # trusts dbtable._version to know whether its cached chunks are
            # still good -- see Dbtable._bump_version()'s docstring. Until
            # this line, only append_row/append_rows/delete_row/delete_rows/
            # clear bumped it (all row-*count*-changing ops); a plain value
            # UPDATE left every existing Dblist cache silently serving
            # pre-update values for that row, potentially forever in a
            # process that only ever updates rows without also appending or
            # deleting any. See tests/db_units/test_dbunits.py::
            # TestDirectDbtableBypass for the equivalent append/delete
            # regression tests this mirrors, and
            # test_regression_direct_update_row_is_not_served_stale
            # for this one specifically.
            dbtable._bump_version()
        return ok

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
        # Logical POINT field name -> its physical (x_column, y_column) pair
        # -- see the "Geo-spatial (POINT) fields" block comment near the top
        # of this module. Every SQL-building / row-conversion method below
        # that touches self._all_columns is written in terms of this map so
        # that, from the outside, a POINT field is still exactly one column.
        self.point_fields: dict[str, tuple[str, str]] = {
            name: _point_columns(name)
            for name, sql_type in self.table_fields.items()
            if sql_type.upper() == GEO_TYPE
        }
        # Bumped by every method that changes row count directly (append_row,
        # append_rows, delete_row, delete_rows, clear). self.list (a Dblist)
        # compares this against its own last-synced value to detect when its
        # chunk cache was left behind by a mutation that didn't go through
        # the Dblist API -- see Dblist.get_delta_chunk.
        self._version = 0
        self.init_list()

    def _bump_version(self) -> None:
        self._version += 1

    # ── internal helpers ─────────────────────────────────────────────────── #

    def _physical_all_columns(self) -> list[str]:
        """Flat physical-column list backing self._all_columns -- every
        regular column unchanged, every POINT field expanded to its (x, y)
        pair. This is the actual column list any SQL statement needs; it's
        also what n_self must count in calc_linked_rows() below, since a
        JOIN's ``a.*``-equivalent column count is physical, not logical."""
        cols = []
        for name in self._all_columns:
            if name in self.point_fields:
                cols.extend(self.point_fields[name])
            else:
                cols.append(name)
        return cols

    def _select_cols(self) -> str:
        """Unqualified column list for simple SELECT … FROM [{id}]."""
        return ", ".join(f"[{c}]" for c in self._physical_all_columns())

    def _aliased_select_cols(self, alias: str = "a") -> str:
        """Alias-qualified column list for JOINs to avoid ambiguous 'ID'."""
        return ", ".join(f"{alias}.[{c}]" for c in self._physical_all_columns())

    def _row_to_list(self, row) -> list:
        """Convert a sqlite3.Row to a typed Python list, applying converters.

        A POINT field consumes two physical values (its _x/_y pair) and
        produces one logical [x, y] list entry (None if either half is
        NULL). For a real sqlite3.Row this is done by column name, so it
        doesn't matter where in the query's physical column order the pair
        actually falls (e.g. append_rows()'s ``RETURNING *``, whose column
        order follows table-creation order, not self._all_columns order).
        The plain-sequence fallback (row is a bare list/tuple) instead
        consumes positionally in self._physical_all_columns() order, which
        every caller that can produce a bare sequence here builds its SELECT
        column list from, so the two stay in lock-step.
        """
        result = []
        is_row = isinstance(row, sqlite3.Row)
        pos = 0
        for key in self._all_columns:
            if key in self.point_fields:
                x_col, y_col = self.point_fields[key]
                if is_row:
                    x_val, y_val = row[x_col], row[y_col]
                else:
                    x_val, y_val = row[pos], row[pos + 1]
                    pos += 2
                result.append(None if x_val is None or y_val is None else [x_val, y_val])
            else:
                val = row[key] if is_row else row[pos]
                if not is_row:
                    pos += 1
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

    def index_of_id(self, row_id: int) -> int:
        """0-based position of the row with this ID under the table's
        default ORDER BY ID ordering.

        Row IDs (SQLite AUTOINCREMENT primary keys) and row *positions*
        (a row's offset in the default listing - what Dblist/iiid/Table
        .value actually index by) are different numbering schemes that
        happen to coincide only for a table that has never had a row
        deleted. Anything that computes a position from a known ID (e.g.
        highlighting linked rows against the unfiltered list - see
        link_table_selection_changed's filter=False branch in tables.py)
        must go through here rather than assuming id == position + 1.
        """
        cur = self.db.execute(
            f"SELECT COUNT(*) FROM [{self.id}] WHERE ID < ?", (row_id,)
        )
        row = cur.fetchone() if cur else None
        return row[0] if row else 0

    # ── search ───────────────────────────────────────────────────────────── #

    # Column types where LIKE search makes sense (text-representable).
    # BLOB, JSON and POINT are deliberately absent: not text-representable
    # (BLOB), would false-positive/false-negative match on serialised
    # structure rather than content (JSON), or -- for POINT -- simply have
    # no single physical column to CAST/LIKE against in the first place
    # (see search_within_radius()/search_nearest() for POINT queries).
    _SEARCHABLE_TYPES = {
        "TEXT", "INTEGER", "REAL", "BOOLEAN",
        "DECIMAL", "UUID", "DATE", "TIMESTAMP",
    }

    def _build_search_where(
        self,
        search: str,
        table_alias: str = "",
    ) -> tuple[str, list]:
        """
        Build a ``WHERE (col1 LIKE ? OR col2 LIKE ?)`` fragment and its
        parameter list for a case-insensitive substring search across all
        searchable columns.

        Non-searchable types (BLOB, JSON) are skipped to avoid false
        positives and SQLite LIKE errors on binary data.

        Args:
            search:      The search string supplied by the client.
            table_alias: Optional table alias prefix (e.g. ``"a."``).

        Returns:
            (where_fragment, params)
            where_fragment is empty string ``""`` when *search* is blank.
        """
        if not search:
            return "", []

        pattern = f"%{search}%"
        prefix = f"{table_alias}." if table_alias else ""

        conditions = []
        params: list = []
        for col in self.node_columns:
            col_type = self.table_fields.get(col, "TEXT").upper()
            if col_type in self._SEARCHABLE_TYPES:
                conditions.append(f"CAST({prefix}[{col}] AS TEXT) LIKE ?")
                params.append(pattern)

        if not conditions:
            return "", []

        return "(" + " OR ".join(conditions) + ")", params

    def search_rows(self, search: str) -> "Dblist":
        """
        Return a *Dblist* (cache mode) of every row whose searchable columns
        contain *search* as a case-insensitive substring.

        Rows are fetched in a single paginated query capped at
        ``self.limit`` to avoid loading an unbounded result set into memory
        at once.  The ``Dblist.cache`` holds the matching rows; the caller
        can compare ``len(result)`` against ``self.limit`` to detect
        truncation and optionally inform the UI.

        Returns an empty Dblist when *search* is blank (fallback to the
        caller to reload the normal list).
        """
        where, params = self._build_search_where(search)
        if not where:
            return Dblist(self, cache=[])

        cur = self.db.execute(
            f"SELECT {self._select_cols()} FROM [{self.id}] "
            f"WHERE {where} "
            f"ORDER BY ID LIMIT ?",
            params + [self.limit],
        )
        rows = [self._row_to_list(r) for r in cur.fetchall()] if cur else []
        return Dblist(self, cache=rows)

    # ── geo-spatial search ──────────────────────────────────────────────── #
    #
    # Both methods below implement "Способ 1 + Способ 2": a cheap
    # equirectangular bounding-box pre-filter that lets SQLite use the
    # (y, x) index created in Database.create_table() for a plain B-Tree
    # range scan, followed by an exact haversine_km() cut/sort on the much
    # smaller surviving candidate set. See the module-level comment above
    # haversine_km() and docs/persistent_tables.md §11 for the full
    # rationale and its trade-offs (in particular: the bounding box is a
    # locally-flat approximation, so accuracy degrades for very large radii
    # or latitudes near ±90°, and it does not handle antimeridian (±180°
    # longitude) wraparound).

    def _point_field_columns(self, field: str) -> tuple[str, str]:
        if field not in self.point_fields:
            raise ValueError(
                f"'{field}' is not a POINT field on table '{self.id}'. "
                f"POINT fields: {list(self.point_fields) or '(none)'}"
            )
        return self.point_fields[field]

    def search_within_radius(
        self,
        field: str,
        lat: float,
        lng: float,
        radius_km: float,
        limit: int = None,
        where: str = "",
        params: tuple = (),
    ) -> "Dblist":
        """
        Rows whose POINT column *field* lies within *radius_km* kilometres
        of (lat, lng), nearest first. Each returned row is one longer than
        self._all_columns: the trailing element is the row's distance from
        (lat, lng) in kilometres (a float), so this is only combinable with
        assign_row()/append_row() etc. after dropping that last element.

        *where*/*params* add an extra, already-parameterised SQL condition
        (e.g. ``where="status = ? AND price <= ?", params=("ACTIVE", 1500)``)
        ANDed onto the radius filter -- mirroring the reference query this
        feature was modelled on (geo + capability + price in one pass).
        """
        x_col, y_col = self._point_field_columns(field)

        dlat = radius_km / _KM_PER_DEGREE_LAT
        coslat = max(math.cos(math.radians(lat)), _MIN_COS_LAT)
        dlng = radius_km / (_KM_PER_DEGREE_LAT * coslat)

        dist_sql = f"haversine_km([{y_col}], [{x_col}], ?, ?)"
        extra = f" AND ({where})" if where else ""
        lim = limit if limit else self.limit

        query = (
            f"SELECT {self._select_cols()}, {dist_sql} AS _distance_km "
            f"FROM [{self.id}] "
            f"WHERE [{y_col}] BETWEEN ? AND ? "
            f"  AND [{x_col}] BETWEEN ? AND ? "
            f"  AND {dist_sql} <= ?"
            f"{extra} "
            f"ORDER BY _distance_km ASC "
            f"LIMIT ?"
        )
        query_params = (
            lat, lng,                     # SELECT ... AS _distance_km
            lat - dlat, lat + dlat,       # y (lat) BETWEEN
            lng - dlng, lng + dlng,       # x (lng) BETWEEN
            lat, lng, radius_km,          # AND haversine_km(...) <= radius_km
            *params,
            lim,
        )
        cur = self.db.execute(query, query_params)
        rows = []
        if cur:
            for r in cur.fetchall():
                row = self._row_to_list(r)
                row.append(r["_distance_km"])
                rows.append(row)
        return Dblist(self, cache=rows)

    def search_nearest(
        self,
        field: str,
        lat: float,
        lng: float,
        k: int = 10,
        initial_radius_km: float = 5.0,
        max_radius_km: float = 500.0,
        where: str = "",
        params: tuple = (),
    ) -> "Dblist":
        """
        The *k* rows whose POINT column *field* is closest to (lat, lng),
        nearest first (expanding-ring search_within_radius(): starts at
        initial_radius_km and doubles until >= k rows are found or
        max_radius_km is reached). Same trailing-distance row shape as
        search_within_radius().

        May return fewer than *k* rows if the table has fewer than *k*
        matches within max_radius_km -- that cap keeps a sparse area from
        silently degrading into a full-table scan; raise it if that
        trade-off doesn't suit your data.
        """
        radius = min(initial_radius_km, max_radius_km)
        result = self.search_within_radius(field, lat, lng, radius, limit=k, where=where, params=params)
        while len(result) < k and radius < max_radius_km:
            radius = min(radius * 2, max_radius_km)
            result = self.search_within_radius(field, lat, lng, radius, limit=k, where=where, params=params)
        return result

    # ── write ────────────────────────────────────────────────────────────── #

    def assign_row(self, row_array: list) -> bool:
        """Update a DB row from a list.  Last element must be the row's DB ID."""
        row_id = row_array[-1]
        props = {name: row_array[i] for i, name in enumerate(self.node_columns)}
        return self.db.update_row(self.id, row_id, props)

    def append_row(self, row) -> list | None:
        """Insert a single row (list, dict, or dataclass instance); return
        the stored row with ID.

        A dataclass instance is matched by *field name* against the
        table's columns -- same as the dict form (dataclasses.fields(row)
        turned into {name: value}) -- not by position like tables.py's
        (unrelated) rows= convention for a non-persistent Table: a DB
        table's columns are already named (`fields={...}`), so name-based
        matching is the natural, unambiguous fit here, with no `headers`
        to line positions up against in the first place.
        """
        if isinstance(row, list):
            props = {
                name: row[i]
                for i, name in enumerate(self.node_columns)
                if i < len(row) and row[i] is not None
            }
        elif isinstance(row, dict):
            props = {k: v for k, v in row.items() if v is not None}
        elif dataclasses.is_dataclass(row) and not isinstance(row, type):
            props = {
                f.name: getattr(row, f.name)
                for f in dataclasses.fields(row)
                if getattr(row, f.name) is not None
            }
        else:
            raise TypeError(f"row must be list, dict, or dataclass instance, got {type(row).__name__}")

        props = _expand_point_props(props, self.point_fields)

        if props:
            cols         = ", ".join(f"[{k}]" for k in props)
            placeholders = ", ".join("?" for _ in props)
            values       = [_adapt_value(v) for v in props.values()]
            cur = self.db.execute(
                f"INSERT INTO [{self.id}] ({cols}) VALUES ({placeholders})", values
            )
        else:
            # Every column is either absent or explicitly None -- e.g. the
            # "add blank row" UI flow (tables.py's append_table_row) inserts
            # [None] * len(headers) for the user to fill in cell-by-cell
            # afterwards. "INSERT INTO t () VALUES ()" is invalid SQLite
            # syntax (near ")": syntax error), so use the dedicated
            # all-defaults form instead.
            cur = self.db.execute(f"INSERT INTO [{self.id}] DEFAULT VALUES")
        if cur is None:
            return None
        new_id = cur.lastrowid
        self.length += 1
        self._bump_version()

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
            elif dataclasses.is_dataclass(row) and not isinstance(row, type):
                # Matched by field name, same as the dict form above (and
                # for the same reason append_row's docstring gives) -- not
                # by position.
                dicts.append({f.name: getattr(row, f.name) for f in dataclasses.fields(row)})
            else:
                raise TypeError(f"Unsupported row type: {type(row)}")

        if self.point_fields:
            dicts = [_expand_point_props(d, self.point_fields) for d in dicts]

        # Union of every key seen across *all* rows, not just dicts[0]: rows
        # may be dicts with different key sets, or lists of different
        # lengths (each becomes a dict with only its own present indices —
        # see the comprehension above). Using only dicts[0].keys() silently
        # dropped any column that the first row happened not to populate,
        # even though a later row did (missing keys default to NULL via
        # d.get(c) below, so nothing is lost either way now).
        cols = list(dict.fromkeys(k for d in dicts for k in d))
        if cols:
            col_str      = ", ".join(f"[{c}]" for c in cols)
            placeholders = ", ".join("?" for _ in cols)
            sql = (
                f"INSERT INTO [{self.id}] ({col_str}) "
                f"VALUES ({placeholders}) RETURNING *"
            )
        else:
            # Every row in the batch is an empty dict -- e.g. bulk-adding
            # several blank rows. "INSERT INTO t () VALUES ()" is invalid
            # SQLite syntax, so use the dedicated all-defaults form instead
            # (see append_row()'s identical guard for the single-row case).
            sql = f"INSERT INTO [{self.id}] DEFAULT VALUES RETURNING *"

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
                    params = tuple(_adapt_value(d.get(c)) for c in cols) if cols else ()
                    cur = self.db._conn.execute(sql, params)
                    raw = cur.fetchone()
                    if raw is not None:
                        inserted.append(self._row_to_list(raw))
        except sqlite3.Error as e:
            self.db.message_logger(f"append_rows failed: {e}")
            return []

        self.length += len(inserted)
        self._bump_version()
        return inserted

    def delete_row(self, row_id: int) -> bool:
        """
        Delete a row by its DB primary key.

        Receives the actual ID (extracted from row[-1] by Dblist.__delitem__),
        never a list offset.  This is correct even when the ID sequence has
        gaps from prior deletions.

        length/version are only updated when a row was *actually* removed
        (checked via cursor.rowcount) — a non-matching row_id (already
        deleted, wrong ID, ...) is a no-op, not a phantom decrement.
        """
        result = self.db.execute(
            f"DELETE FROM [{self.id}] WHERE ID = ?", (row_id,)
        )
        if result is not None and result.rowcount:
            self.length -= 1
            self._bump_version()
        return result is not None

    def delete_rows(self, ids) -> bool:
        ids = list(ids)
        ph  = ", ".join("?" for _ in ids)
        result = self.db.execute(
            f"DELETE FROM [{self.id}] WHERE ID IN ({ph})", ids
        )
        if result is not None:
            self.length -= result.rowcount
            if result.rowcount:
                self._bump_version()
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
            self._bump_version()
        return result is not None

    # ── relation helpers (junction tables) ───────────────────────────────── #

    def set_fk(self, row_id: int, link_id: int | None) -> bool:
        """Many-to-one: set link_id for a single row."""
        return self.db.execute(
            f"UPDATE [{self.id}] SET [{self.LINK_ID}] = ? WHERE ID = ?",
            (_adapt_value(link_id), row_id),
        ) is not None

    def clear_fk(self, row_id: int) -> bool:
        """Many-to-one: clear link_id (set NULL) for a single row."""
        return self.set_fk(row_id, None)

    def default_index_name2(self, link_table: str) -> str:
        return f"{self.id}2{link_table}"

    def _existing_tables(self) -> set[str]:
        cur = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        return {row[0] for row in cur.fetchall()}

    def row_to_dict(self, row: list, extra_fields: tuple = ()) -> dict:
        """
        Label a row (the plain [*fields, ID] list every read path on this
        class returns -- get()/find_one() convert into this internally,
        but search_within_radius()/search_nearest() append one more
        column, distance_km, beyond [*fields, ID] and so can't reuse those
        two directly) into a {field_name: value, ..., 'id': ...} dict:

            for row in offers.search_within_radius('position', lat, lng, 10):
                offer = offers.row_to_dict(row, extra_fields=('distance_km',))

        extra_fields names any columns appended after ID, in order, for
        exactly this kind of case.
        """
        result = {name: row[i] for i, name in enumerate(self.node_columns)}
        result["id"] = row[len(self.node_columns)]
        for i, name in enumerate(extra_fields):
            result[name] = row[len(self.node_columns) + 1 + i]
        return result

    def _raw_to_dict(self, raw) -> dict:
        """Shared by get()/find_one(): a fetchone() row -> row_to_dict(),
        going through the same logical-column machinery (_row_to_list) as
        every other public read path, so a POINT field comes back as one
        [lng, lat] entry here exactly like it does from self.list/
        search_rows/etc."""
        return self.row_to_dict(self._row_to_list(raw))

    def get(self, row_id: int) -> dict | None:
        """
        Fetch one row by ID, fresh from the database -- never from a
        cached Dblist chunk (see Dblist._sync_cache/self.list, which is a
        *view* meant for paginated/GUI consumption and is only as fresh as
        the last operation that bumped self._version).

        Returns {field_name: value, ..., 'id': row_id}, or None if no row
        with this ID exists. This is the direct, no-surprises counterpart
        to append_row() for reading a single row backend code already
        knows the ID of -- e.g. a webhook handler re-checking a status
        flag another request may just have written.
        """
        cur = self.db.execute(
            f"SELECT {self._select_cols()} FROM [{self.id}] WHERE ID = ?", (row_id,)
        )
        raw = cur.fetchone() if cur else None
        return self._raw_to_dict(raw) if raw is not None else None

    def find_one(self, **field_equals) -> dict | None:
        """
        Fetch the first row where every given field exactly equals the
        given value (AND-combined), fresh from the database. Returns None
        if nothing matches.

            actors.find_one(vapi_call_id=call_id)

        Narrower than search_rows() on purpose: search_rows() is a
        case-insensitive *substring* match across every text column (built
        for a GUI search box), which is the wrong tool for "does a row
        with exactly this key exist" -- a capability value that happens to
        be a substring of another one would match both. Every value here
        is bound as a query parameter (never interpolated), so this is
        safe to call with arbitrary field values; field *names* still go
        through normal Python keyword-argument rules, not user input.
        """
        where = " AND ".join(f"[{k}] = ?" for k in field_equals)
        cur = self.db.execute(
            f"SELECT {self._select_cols()} FROM [{self.id}] WHERE {where} LIMIT 1",
            tuple(field_equals.values()),
        )
        raw = cur.fetchone() if cur else None
        return self._raw_to_dict(raw) if raw is not None else None

    def update(self, row_id: int, fields: dict) -> dict | None:
        """
        Patch specific fields on one row by ID; returns the fresh row (see
        get()) on success, or None if the row doesn't exist / the update
        failed.

        Unlike Dblist.update_cell() (delta/cell address a position in a
        *currently rendered GUI page*, not a stable row identifier) and
        assign_row() (needs the entire row passed back as a positional
        list), this takes just the ID and only the fields actually
        changing -- the natural shape for backend code (a webhook handler,
        a scheduled job, ...) that knows which row and which fields, not
        which page happens to be open in someone's browser right now.
        """
        if not self.db.update_row(self.id, row_id, fields):
            return None
        return self.get(row_id)

    LINK_ID = "link_id"   # fixed FK column name for many-to-one relations

    def setup_fk(self, tname: str) -> None:
        """
        Many-to-one: ensure [link_id] FK column exists in this table.

        Adds [link_id INTEGER REFERENCES [{tname}](ID)] if absent.
        No migration — the column is simply added when missing.
        """
        existing = self.db.get_table_fields(self.id) or {}
        if self.LINK_ID not in existing:
            self.db.execute(
                f"ALTER TABLE [{self.id}] "
                f"ADD COLUMN [{self.LINK_ID}] INTEGER REFERENCES [{tname}](ID)"
            )
            self.table_fields[self.LINK_ID] = "INTEGER"
            self.node_columns.append(self.LINK_ID)
            self._all_columns = self.node_columns + ["ID"]

    def setup_junction(
        self,
        tname: str,
        fields: dict,
        relname: str = None,
    ) -> tuple[str, dict]:
        """
        Many-to-many: ensure junction table exists with optional payload fields.

        Returns (junction_table_name, normalised_fields_dict).
        """
        if not relname:
            relname = self.default_index_name2(tname)

        fields = normalize_field_types(fields)

        existing = self.db.get_table_fields(relname)
        if existing is not None:
            # Compare against payload fields only: get_table_fields() always
            # includes the junction's own src_id/tgt_id structural columns,
            # which never appear in the caller's `fields` (just the extra
            # payload, e.g. {'qty': int}). Comparing the raw dicts meant
            # existing.keys() ({'src_id','tgt_id','qty'}) could never equal
            # fields.keys() ({'qty'}), so "schema unchanged" never matched
            # and every call dropped and recreated the table -- silently
            # destroying every existing link on every setup_junction() call
            # (tables.py's Table.__init__ calls this on every construction
            # of a many-to-many linked table, i.e. on every screen load).
            existing_payload = {
                k: v for k, v in existing.items() if k not in ("src_id", "tgt_id")
            }
            if _equal_field_dicts(existing_payload, fields):
                return relname, existing_payload
            # Schema changed — drop and recreate.
            self.db.delete_table(relname)

        extra = (
            ", " + ", ".join(
                f"[{col}] {type_}" for col, type_ in _physical_field_columns(fields)
            )
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

    def calc_linked_rows_fk(self, link_ids, search: str = "") -> "Dblist":
        """Many-to-one: return rows WHERE link_id IN (link_ids).

        When *search* is non-empty, only rows whose searchable columns
        contain the search string (case-insensitive substring) are included.
        """
        ids = list(link_ids)
        ph  = ", ".join("?" for _ in ids)

        search_where, search_params = self._build_search_where(search)
        extra_where = f" AND {search_where}" if search_where else ""

        cur = self.db.execute(
            f"SELECT {self._select_cols()} FROM [{self.id}] "
            f"WHERE [{self.LINK_ID}] IN ({ph}){extra_where} ORDER BY ID",
            ids + search_params,
        )
        lst = [self._row_to_list(r) for r in cur.fetchall()] if cur else []
        return Dblist(self, cache=lst)

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

        # Junction tables have no Dbtable of their own to carry a
        # point_fields mapping, so it's looked up on demand from the
        # (already POINT-folding-aware) physical schema whenever payload
        # fields are actually supplied.
        if link_fields:
            junction_fields = self.db.get_table_fields(link_index_name, remove_id=False) or {}
            point_fields = {
                name: _point_columns(name)
                for name, t in junction_fields.items()
                if t.upper() == GEO_TYPE
            }
            link_fields = _expand_point_props(link_fields, point_fields)

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

        if link_ids is not None:
            ids = list(link_ids)
            if not ids:
                # Explicit empty ID list: nothing to delete, not "fall
                # through to the source_ids/link_node_id branch" (which
                # would previously raise TypeError when those were also
                # unset, since link_ids=[] is falsy just like link_ids=None).
                return True
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
        """Return a Dblist of source rows linked to any row in *link_ids*.

        When *search* is non-empty, only rows whose searchable columns
        contain the search string (case-insensitive substring) are included.

        When *include_rels* is True, each returned row is extended with the
        junction table's own payload fields (as set up by setup_junction),
        followed by the junction row's own ID -- e.g. for a many-to-many
        link carrying a "qty" field, each row becomes
        [<node fields...>, <node ID>, <qty>, <junction row ID>].
        src_id/tgt_id are omitted since callers already know both ends.
        """
        ids = list(link_ids)
        ph  = ", ".join("?" for _ in ids)

        # Junction columns in schema order: src_id, tgt_id, <payload...>, ID.
        # Fetched *before* rel_cols so a missing/renamed junction table is
        # reported the same way for every row rather than only once fetchall
        # runs.
        junction_fields: dict = {}
        if include_rels:
            junction_fields = self.db.get_table_fields(index_name, remove_id=False) or {}
        rel_cols = ", r.*" if include_rels else ""

        search_where, search_params = self._build_search_where(search, table_alias="a")
        extra_where = f" AND {search_where}" if search_where else ""

        query = (
            f"SELECT {self._aliased_select_cols('a')}{rel_cols} "
            f"FROM [{self.id}] a "
            f"JOIN [{index_name}] r ON r.src_id = a.[ID] "
            f"JOIN [{target_table}] b ON r.tgt_id = b.[ID] "
            f"WHERE b.[ID] IN ({ph}){extra_where} "
            f"ORDER BY a.[ID] ASC"
        )
        cur = self.db.execute(query, ids + search_params)
        rows = cur.fetchall() if cur else []

        if include_rels and junction_fields:
            # n_self must be the *physical* column count of "a" in the SELECT
            # (self._aliased_select_cols('a')) -- a POINT field on self makes
            # that wider than len(self._all_columns), which would otherwise
            # misalign every raw_rel slice below by however many POINT
            # fields precede it.
            n_self = len(self._physical_all_columns())
            # Everything after src_id/tgt_id: payload fields, then ID.
            payload_fields = {
                k: t for k, t in junction_fields.items() if k not in ("src_id", "tgt_id")
            }
            # A POINT payload field is likewise two physical values wide.
            physical_payload_names = []
            for k, t in payload_fields.items():
                if t.upper() == GEO_TYPE:
                    physical_payload_names.extend(_point_columns(k))
                else:
                    physical_payload_names.append(k)
            lst = []
            for r in rows:
                base = self._row_to_list(r)
                raw_rel = list(r)[n_self + 2 : n_self + 2 + len(physical_payload_names)]
                rel_map = dict(zip(physical_payload_names, raw_rel))
                for k, t in payload_fields.items():
                    if t.upper() == GEO_TYPE:
                        x_col, y_col = _point_columns(k)
                        xv, yv = rel_map.get(x_col), rel_map.get(y_col)
                        base.append(None if xv is None or yv is None else [xv, yv])
                    else:
                        base.append(_convert_value(rel_map.get(k), t))
                lst.append(base)
        else:
            lst = [self._row_to_list(r) for r in rows]
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