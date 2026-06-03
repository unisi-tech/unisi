# Copyright © 2024 UNISI Tech. All rights reserved.
"""
kdb.py — Kuzu graph-database backend for the UNISI db layer.

Cymple has been removed entirely:
  - It did not support parameterised queries (injection risk).
  - Its Properties serialiser produced wrong Cypher for bool values
    (Python ``True`` → ``True`` instead of the required ``true``).
  - All generated queries were simple enough to write as plain f-strings
    with a small set of helper functions defined here.
"""
import kuzu, shutil, os, re
from datetime import date, datetime
from .dbunits import Dblist


# ── Cypher literal helpers ────────────────────────────────────────────────────

def cypher_literal(value) -> str:
    """
    Render a Python value as a Cypher inline literal.

    Handles: None, bool, int, float, str, date, datetime, list/tuple.
    All other types are coerced to a quoted string via str().

    Unlike cymple.typedefs.Properties, booleans are correctly lowercased
    (``true`` / ``false``) per the openCypher / Kuzu specification.
    Strings are single-quoted with backslash and single-quote escaping.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"    # openCypher: must be lowercase
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, datetime):
        return f"datetime('{value.isoformat()}')"
    if isinstance(value, date):
        return f"date('{value.isoformat()}')"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(cypher_literal(v) for v in value) + "]"
    # Fallback: quoted string representation
    return f"'{str(value)}'"


def props_literal(d: dict) -> str:
    """
    Render a dict as a Cypher property map: ``{key: val, ...}``.
    Empty dict → empty string (no braces).
    """
    if not d:
        return ""
    parts = ", ".join(f"{k}: {cypher_literal(v)}" for k, v in d.items())
    return "{" + parts + "}"


# ── Cypher query builders (replace cymple QueryBuilder) ──────────────────────

def cypher_match_id(table: str, row_id: int) -> str:
    """MATCH (a: T) WHERE a.ID = <row_id>"""
    return f"MATCH (a: {table}) WHERE a.ID = {row_id}"


def cypher_read_rows(table: str, limit: int, skip: int = 0) -> str:
    """MATCH (a: T) RETURN a.* ORDER BY a.ID [SKIP n] LIMIT n"""
    q = f"MATCH (a: {table}) RETURN a.* ORDER BY a.ID"
    if skip:
        q += f" SKIP {skip}"
    q += f" LIMIT {limit}"
    return q


def cypher_create_node(table: str, props: dict) -> str:
    """CREATE (a: T {props}) RETURN a.*"""
    return f"CREATE (a: {table} {props_literal(props)}) RETURN a.*"


def cypher_unwind_create(table: str, rows: list[dict], node_columns: list[str]) -> str:
    """
    WITH [{...}, {...}] AS rows
    UNWIND rows AS row
    CREATE (n: T {col: row.col, ...})
    RETURN n.*
    """
    rows_str = ", ".join(props_literal(r) for r in rows)
    col_map  = ", ".join(f"{p}: row.{p}" for p in node_columns)
    return (
        f"WITH [{rows_str}] AS rows "
        f"UNWIND rows AS row "
        f"CREATE (n: {table} {{{col_map}}}) "
        f"RETURN n.*"
    )


# ── General helpers ───────────────────────────────────────────────────────────

def get_default_args(func) -> dict:
    import inspect
    sig = inspect.signature(func)
    return {
        k: v.default
        for k, v in sig.parameters.items()
        if v.default is not inspect.Parameter.empty
    }


def equal_fields_dicts(dict1: dict, dict2: dict) -> bool:
    return dict1.keys() == dict2.keys() and all(
        dict1[k].lower() == dict2[k].lower() for k in dict1
    )


def is_modifying_query(cypher_query: str) -> bool:
    pattern = (
        r"\b(create|delete|detach\s*delete|set|merge|remove"
        r"|call\s+\w+(\.\w+)?\s+yield|foreach)\b"
    )
    return bool(re.search(pattern, cypher_query.lower()))


def kuzu_data_type(value) -> str:
    match value:
        case bool():           return "BOOLEAN"
        case int():            return "INT64"
        case float():          return "DOUBLE"
        case str():            return "STRING"
        case datetime():       return "TIMESTAMP"
        case date():           return "DATE"
        case bytes():          return "BLOB"
        case list() | tuple(): return "LIST"
        case _:                return ""


number_types = {"DOUBLE", "INT64"}


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    def __init__(self, dbpath: str, message_logger=print) -> None:
        self.tables: dict[str, "Dbtable"] = {}   # per-instance, not class-level
        self.db   = kuzu.Database(dbpath)
        self.conn = kuzu.Connection(self.db)
        self.message_logger = message_logger
        self.table_params   = get_default_args(self.get_table)

    # ── execution ────────────────────────────────────────────────────────── #

    def execute(self, query: str, ignore_exception: bool = False):
        try:
            result = self.conn.execute(query)
        except Exception as e:
            if not ignore_exception:
                self.message_logger(e)
            return None
        return True if result is None else result

    @staticmethod
    def delete(dir_path: str) -> None:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)

    # ── schema ───────────────────────────────────────────────────────────── #

    @property
    def table_names(self) -> list[str]:
        return self.conn._get_node_table_names()

    def get_table_fields(
        self, table_name: str, remove_id: bool = True
    ) -> dict | None:
        result = self.qlist(
            f"CALL table_info('{table_name}') RETURN *;",
            ignore_exception=True,
        )
        if result is not None:
            return {
                info[1]: info[2]
                for info in result
                if not remove_id or info[1] != "ID"
            }

    def delete_table(self, table_name: str) -> bool:
        return self.execute(f"DROP TABLE {table_name};") is not None

    # ── table factory ────────────────────────────────────────────────────── #

    def get_table(
        self,
        id: str       = None,
        limit: int    = 100,
        headers: list = None,
        rows: list    = None,
        fields: dict  = None,
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
                        ktype = kuzu_data_type(cell)
                        if ktype:
                            if types[j] is None:
                                types[j] = ktype
                            elif types[j] != ktype:
                                if types[j] in number_types and ktype in number_types:
                                    types[j] = "DOUBLE"
                                else:
                                    self.message_logger(
                                        f"Conflicting types for '{id}' col {j}: "
                                        f"{types[j]} vs {ktype}",
                                        "warning",
                                    )
                                    return None
            if None in types:
                self.message_logger(
                    f"Cannot infer type for column '{headers[types.index(None)]}'"
                )
                return None
            fields = {headers[i]: t for i, t in enumerate(types)}

        table_fields = self.get_table_fields(id)
        if table_fields is not None:
            if fields is not None and not equal_fields_dicts(table_fields, fields):
                if self.delete_table(id):
                    self.message_logger(
                        f"Node table '{id}' dropped due to schema mismatch.",
                        "warning",
                    )
            else:
                return self.tables.get(id) or Dbtable(id, self, limit, table_fields)

        return self.create_table(id, fields, limit, rows)

    def get_table_params(self, params: dict) -> dict:
        return {k: v for k, v in params.items() if k in self.table_params}

    def set_db_list(self, gui_table):
        table = self.get_table(**self.get_table_params(gui_table.__dict__))
        gui_table.rows = table.list

    def create_table(
        self, id: str, fields: dict, limit: int = 100, rows=None
    ) -> "Dbtable":
        specs = ", ".join(f"{prop} {type_}" for prop, type_ in fields.items())
        self.execute(
            f"CREATE NODE TABLE {id}({specs}, ID SERIAL, PRIMARY KEY(ID))"
        )
        table = Dbtable(id, self, limit, fields)
        if rows:
            table.list.extend(rows)
        return table

    def update_row(
        self,
        table_id: str,
        row_id:   int,
        props:    dict,
        in_node:  bool = True,
    ) -> bool:
        set_parts = []
        for key, value in props.items():
            set_parts.append(f"a.{key} = {cypher_literal(value)}")
        set_clause = "SET " + ", ".join(set_parts)
        if in_node:
            query = f"MATCH (a: {table_id}) WHERE a.ID = {row_id} {set_clause}"
        else:
            query = (
                f"MATCH ()-[a: {table_id}]->() WHERE a.ID = {row_id} {set_clause}"
            )
        return self.execute(query) is not None

    # ── query helpers ────────────────────────────────────────────────────── #

    def qlist(self, query, func=None, ignore_exception: bool = False) -> list | None:
        answer = self.execute(query, ignore_exception)
        if answer and answer is not True:
            result = []
            while answer.has_next():
                value = answer.get_next()
                result.append(func(value) if func else value)
            return result

    def qiter(self, query, func=None, ignore_exception: bool = False):
        answer = self.execute(query, ignore_exception)
        if answer and answer is not True:
            while answer.has_next():
                value = answer.get_next()
                yield func(value) if func else value


# ── Dbtable ───────────────────────────────────────────────────────────────────

class Dbtable:
    def __init__(
        self,
        id:           str,
        db:           Database,
        limit:        int  = 100,
        table_fields: dict = None,
    ) -> None:
        self.db           = db
        db.tables[id]     = self
        self.id           = id
        self.table_fields = table_fields
        self.limit        = limit
        self.node_columns: list[str] = list(
            db.conn._get_node_property_names(id).keys()
        )[:-1]
        self.init_list()

    # ── relation helpers ─────────────────────────────────────────────────── #

    @property
    def rel_table_names(self) -> list[dict]:
        return self.db.conn._get_rel_table_names()

    def default_index_name2(self, link_table: str) -> str:
        return f"{self.id}2{link_table}"

    def calc_linked_rows(
        self,
        index_name:   str,
        link_ids,
        target_table: str,
        include_rels: bool = False,
        search:       str  = "",
    ) -> Dblist:
        condition = f"b.ID in {list(link_ids)}"
        rel_info  = ", r.*" if include_rels else ""
        query = (
            f"MATCH (a:{self.id})-[r:{index_name}]->(b:{target_table}) "
            f"WHERE {condition} "
            f"RETURN a.*{rel_info} "
            f"ORDER BY a.ID ASC"
        )
        return Dblist(self, cache=self.db.qlist(query) or [])

    def get_rel_fields2(
        self,
        tname:  str,
        fields: dict = None,
        relname: str = None,
    ) -> tuple[str, dict]:
        if not relname:
            relname = self.default_index_name2(tname)

        rel_table_fields = self.db.get_table_fields(relname)
        if isinstance(rel_table_fields, dict):
            if isinstance(fields, dict):
                if equal_fields_dicts(rel_table_fields, fields):
                    return relname, rel_table_fields
                self.db.delete_table(relname)
            else:
                fields = rel_table_fields
        elif fields is None:
            fields = {}

        # rel_table_names returns a fresh list each call — never mutate it.
        if relname not in {info["name"] for info in self.rel_table_names}:
            fprops = (
                "".join(f", {field} {type_}" for field, type_ in fields.items())
                if fields else ""
            )
            self.db.execute(
                f"CREATE REL TABLE {relname}"
                f"(FROM {self.id} TO {tname}{fprops}, ID SERIAL)"
            )
        return relname, fields

    def add_link(
        self,
        snode_id:       int,
        link_table:     str,
        tnode_id:       int,
        link_fields:    dict = None,
        link_index_name: str = None,
    ):
        if link_index_name is None:
            link_index_name = self.default_index_name2(link_table)
        if link_fields is None:
            link_fields = {}
        lf_str = props_literal(link_fields)
        query = (
            f"MATCH (a:{self.id}), (b:{link_table}) "
            f"WHERE a.ID = {snode_id} AND b.ID = {tnode_id} "
            f"CREATE (a)-[r:{link_index_name} {lf_str}]->(b) "
            f"RETURN r.*"
        )
        lst = self.db.qlist(query)
        return lst[0] if lst else None

    def add_links(
        self,
        link_table:      str,
        snode_ids,
        tnode_id:        int,
        link_index_name: str = None,
    ) -> list:
        return [
            self.add_link(sid, link_table, tnode_id, link_index_name=link_index_name)
            for sid in snode_ids
        ]

    def delete_link(
        self, link_table_id: str, link_id: int, index_name: str = None
    ):
        if not index_name:
            index_name = self.default_index_name2(link_table_id)
        self.db.execute(
            f"MATCH (:{self.id})-[r:{index_name}]->(:{link_table_id}) "
            f"WHERE r.ID = {link_id} "
            f"DELETE r"
        )

    def delete_links(
        self,
        link_table_id: str,
        link_node_id:  int  = None,
        source_ids          = None,
        link_ids            = None,
        index_name:    str  = None,
    ):
        if not index_name:
            index_name = self.default_index_name2(link_table_id)
        if link_ids:
            condition = f"r.ID in {list(link_ids)}"
        else:
            if not isinstance(source_ids, list):
                source_ids = list(source_ids)
            condition = f"(a.ID in {source_ids}) AND b.ID = {link_node_id}"
        self.db.execute(
            f"MATCH (a:{self.id})-[r:{index_name}]->(b:{link_table_id}) "
            f"WHERE {condition} "
            f"DELETE r"
        )

    # ── core list operations ─────────────────────────────────────────────── #

    def init_list(self):
        rows   = self.read_rows(limit=self.limit)
        length = len(rows)
        if length == self.limit:
            ql = self.db.qlist(f"MATCH (n:{self.id}) RETURN count(n)")
            self.length = ql[0][0]
        else:
            self.length = length
        self.list = Dblist(self, rows)

    def read_rows(self, skip: int = 0, limit: int = 0) -> list:
        return self.db.qlist(
            cypher_read_rows(self.id, limit if limit else self.limit, skip)
        ) or []

    def assign_row(self, row_array: list) -> bool:
        """Update a DB row from a list.  row_array[-1] is the DB primary key."""
        return self.db.update_row(
            self.id,
            row_array[-1],
            {name: value for name, value in zip(self.node_columns, row_array)},
        )

    def delete_row(self, row_id: int) -> bool:
        """
        Delete a single node by its DB primary key.
        Receives row[-1] extracted by Dblist.__delitem__ — never a list offset.
        """
        self.length -= 1
        return self.db.execute(
            f"{cypher_match_id(self.id, row_id)} DETACH DELETE a"
        ) is not None

    def delete_rows(self, ids) -> bool:
        return self.db.execute(
            f"MATCH (a:{self.id}) WHERE a.ID in {list(ids)} DELETE a"
        ) is not None

    def clear(self, detach: bool = False) -> bool:
        suffix = "DETACH DELETE a" if detach else "DELETE a"
        self.length = 0
        return self.db.execute(f"MATCH (a:{self.id}) {suffix}") is not None

    def append_row(self, row) -> list | None:
        """Insert a single row (list or dict); return the stored row with ID."""
        if isinstance(row, list):
            props = {
                name: value
                for name, value in zip(self.node_columns, row)
                if value is not None
            }
        elif isinstance(row, dict):
            props = {k: v for k, v in row.items() if v is not None}
        else:
            raise TypeError(f"row must be list or dict, got {type(row).__name__}")

        answer = self.db.execute(cypher_create_node(self.id, props))
        if answer and answer is not True and answer.has_next():
            self.length += 1
            return answer.get_next()

    def append_rows(self, rows: list) -> list:
        """Bulk-insert via UNWIND; return list of stored rows."""
        if not rows:
            return []
        dicts: list[dict] = []
        for row in rows:
            if isinstance(row, list):
                dicts.append({
                    name: value
                    for name, value in zip(self.node_columns, row)
                })
            elif isinstance(row, dict):
                dicts.append(row)
            else:
                raise TypeError(f"Unsupported row type: {type(row).__name__}")

        self.length += len(dicts)
        return self.db.qlist(
            cypher_unwind_create(self.id, dicts, self.node_columns)
        ) or []
