# UNISI — Persistent Tables

> Developer guide for creating, editing, and relating SQLite-backed tables —
> both programmatically and through the web interface.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Configuration](#2-configuration)
3. [Single Table](#3-single-table)
4. [Many-to-One (Foreign Key)](#4-many-to-one-foreign-key)
5. [Many-to-Many (Junction Table)](#5-many-to-many-junction-table)
6. [The `filter` Flag](#6-the-filter-flag)
7. [Displaying IDs and Relation Columns](#7-displaying-ids-and-relation-columns)
8. [Schema Evolution](#8-schema-evolution)
9. [Full Example — Authors › Books › Genres](#9-full-example--authors--books--genres)
10. [Dbtable API Reference](#10-dbtable-api-reference)
11. [Geo-Spatial Fields](#11-geo-spatial-fields)

---

## 1. Overview

UNISI stores table data in a built-in SQLite database. A **Persistent Table** is
a `Table` instance that has an `id` attribute — UNISI automatically binds it to
an SQLite table with that name. All operations (insert, edit, delete, search) are
synced to the database without writing any SQL by hand.

### Stack layers

```
Web client  →  JSON events  →  Unishare router  →  handler
    →  Table / Dblist  →  Dbtable  →  SQLite
```

Programmatic access follows the same path from `Table` downward.

### Key objects

| Class | Role | Where |
|---|---|---|
| `Table` | GUI component sent to the client | `screens/*.py` |
| `Dblist` | Row list with a transparent DB proxy. Lives at `table.rows` | `dbunits.py` |
| `Dbtable` | Low-level SQLite access. Lives at `table.rows.dbtable` | `db.py` |
| `Database` | SQLite connection. Lives at `Unishare.db` | `db.py` |

---

## 2. Configuration

Set the database path in your application's `config.py`:

```python
# config.py
db_path = 'database/app.db'   # relative to the app root
                               # if the path is a directory, unisi.db is created inside it
```

> **Note:** If `db_path` is not set, persistent tables are unavailable.
> Attempting to create a `Table` with an `id` raises `AssertionError`.

---

## 3. Single Table

### 3.1 Declaration

The only strictly required attribute is `id` (the SQLite table name).
How the schema is determined depends on what else you provide:

| Situation | How the schema is resolved |
|---|---|
| `fields` given | Used directly as the authoritative schema |
| `fields` absent, `rows` given | Column types are **inferred** from the row data; `headers` must also be provided so UNISI can map values to column names |
| Both absent | Table must already exist in SQLite — schema is read from `PRAGMA table_info` |

**Variant 1 — explicit `fields` (recommended for new tables)**

```python
# screens/users.py
from unisi import Table

users = Table('Users',
    id     = 'users',           # SQLite table name
    fields = {                  # schema: name → Python type
        'name'  : str,
        'email' : str,
        'age'   : int,
        'active': bool,
    },
    headers = ['Name', 'Email', 'Age', 'Active'],  # optional display names
    limit   = 200,              # max rows per page (default 100)
)
```

**Variant 2 — schema inferred from `rows` (regular UNISI table rows)**

When `fields` is omitted and `rows` are provided, UNISI scans every cell to
infer the SQLite type. `headers` is required in this case so column names are
known.

```python
users = Table('Users',
    id      = 'users',
    headers = ['Name', 'Email', 'Age', 'Active'],   # required for inference
    rows    = [
        ['Alice', 'alice@example.com', 30, True],
        ['Bob',   'bob@example.com',   25, False],
    ],
    # fields is omitted — types are inferred as TEXT, TEXT, INTEGER, BOOLEAN
)
```

> **Note:** All values in a column must resolve to the same SQLite type.
> If a column contains only `None` values across all seed rows, inference
> fails and UNISI logs an error. Mixed numeric types (`int` + `float`) are
> widened to `REAL`.

**Variant 3 — bind to an existing SQLite table**

If the table already exists in the database and neither `fields` nor `rows`
are provided, UNISI reads the schema directly from SQLite and no migration
is triggered.

```python
# Binds to an existing 'users' table — schema comes from PRAGMA table_info
users = Table('Users', id='users')
```

### 3.2 Supported types

| Python type | SQLite type | Notes |
|---|---|---|
| `str` | TEXT | |
| `int` | INTEGER | |
| `float` | REAL | |
| `bool` | BOOLEAN | Stored as INTEGER 0/1 |
| `bytes` | BLOB | Excluded from search |
| `list` / `tuple` / `dict` | JSON | `json.dumps`. Excluded from search |
| `datetime` | TIMESTAMP | ISO-8601 TEXT |
| `date` | DATE | ISO-8601 TEXT |
| `Decimal` | DECIMAL | String round-trip |
| `uuid.UUID` | UUID | String round-trip |
| `[float, float]` / `(float, float)` | POINT | Geo-spatial point, two REAL columns internally. Excluded from search. See [§11](#11-geo-spatial-fields) |

### 3.3 Adding rows programmatically

**Single row via Dblist**

```python
# Dblist transparently writes to SQLite on append
new_row = users.rows.append([None, None, None, None])
# new_row is a list with the auto-generated ID in the last position
# e.g. [None, None, None, None, 42]  ← 42 = new DB ID
```

**Single row via Dbtable**

```python
dbtable = users.rows.dbtable

# dict variant (recommended — fields are explicit)
row = dbtable.append_row({'name': 'Alice', 'email': 'alice@example.com', 'age': 30, 'active': True})

# list variant (column order must match fields)
row = dbtable.append_row(['Alice', 'alice@example.com', 30, True])
# row = ['Alice', 'alice@example.com', 30, True, 1]  ← ID appended
```

**Bulk insert**

```python
rows = dbtable.append_rows([
    {'name': 'Anna',  'email': 'anna@example.com',  'age': 25, 'active': True},
    {'name': 'Boris', 'email': 'boris@example.com', 'age': 40, 'active': False},
])
# Returns the stored rows with IDs. The entire batch is atomic —
# either all rows are inserted or none.
```

> **Note:** `append_rows` uses `RETURNING *` inside a single transaction,
> so there is no race condition between `INSERT` and `SELECT`.

### 3.4 Editing rows

**Update a cell via Dblist**

```python
# Dblist syncs any item assignment back to SQLite
users.rows[0][1] = 'new@example.com'   # updates the cell and the DB

# Or via update_cell (used internally by the GUI modify handler)
users.rows.update_cell(delta=0, cell=1, value='new@example.com')
```

**Update an entire row via Dbtable**

```python
# assign_row updates the row identified by its ID (last element)
row = users.rows[0]         # ['Alice', 'alice@.com', 30, True, 1]
row[2] = 31                 # change age
dbtable.assign_row(row)     # writes to SQLite
```

**Single-row access from backend code**

`update_cell`'s `delta`/`cell` address a position in whatever page happens
to be rendered in a browser right now, and `assign_row` needs the entire
row passed back as a positional list — both are the right tool when you're
already holding a row from `dbt.list`, but awkward from code that only
knows a row's *ID* and which *field(s)* it wants to touch: a webhook
handler, a scheduled job, a data-migration script. `get`, `find_one`, and
`update` are the direct counterparts for that case — by field name, not
position, and always read fresh from the database rather than through
`dbt.list`'s page cache (see the caching note in [§10](#10-dbtable-api-reference)):

```python
# Fetch one row by ID — {field: value, ..., 'id': ...}, or None
user = dbtable.get(42)

# Fetch the first row matching exact field values (AND-combined) — not a
# substring/LIKE search like search_rows(); every value is a bound query
# parameter
user = dbtable.find_one(email='alice@example.com')

# Patch just the given fields by ID; returns the fresh row, or None if the
# ID doesn't exist
user = dbtable.update(42, {'age': 31})
```

### 3.5 Deleting rows

```python
# Delete one row by its list index
del users.rows[0]

# Delete multiple rows
del users.rows[[0, 2, 5]]

# Low-level — delete by DB ID
dbtable.delete_row(row_id=42)

# Bulk delete by DB IDs
dbtable.delete_rows([1, 2, 3])

# Clear the entire table
dbtable.clear()
```

### 3.6 Web interface

When `edit = True` (default), UNISI registers three GUI handlers automatically:

| Client event | Handler | Action |
|---|---|---|
| `{event: "append"}` | `append_table_row` | Creates an empty row and writes it to the DB |
| `{event: "modify"}` | `accept_cell_value` | Updates a single cell in the DB |
| `{event: "delete"}` | `delete_table_row` | Deletes the selected rows from the DB |

Client JSON examples:

```json
// Add a row
{ "block": "TBlock", "element": "Users", "event": "append" }

// Edit a cell (row 2, column 1, new value)
{ "block": "TBlock", "element": "Users", "event": "modify",
  "value": { "delta": 2, "cell": 1, "value": "new@example.com" } }

// Delete selected row (or an array of indices)
{ "block": "TBlock", "element": "Users", "event": "delete", "value": 2 }
```

### 3.7 Search

The `search` event filters rows by substring match across all text and numeric
columns (`LIKE`). `BLOB`, `JSON`, and `POINT` columns are skipped — for
geo-spatial "find rows near here" queries, use `search_within_radius`/
`search_nearest` instead (see [§11](#11-geo-spatial-fields)).

```json
{ "block": "TBlock", "element": "Users", "event": "search", "value": "alice" }
```

The server updates `table.rows` in-place — only matching rows are shown.
The result is capped at `table.limit` to avoid loading unbounded data.

**Programmatic search via Dbtable:**

```python
result = users.rows.dbtable.search_rows('alice')
# result is a Dblist (cache mode) with the filtered rows
# At most `limit` rows are returned

# Apply the result to the table
users.rows = result
```

> **Note:** An empty search string (`value = ""`) resets the table to its normal
> view — `dbtable.init_list()` is called and `dbtable.list` is restored.

---

## 4. Many-to-One (Foreign Key)

### 4.1 Concept

Many-to-one is a classic FK relationship: multiple rows of a child table
(`orders`) reference a single row of a parent table (`customers`). UNISI
automatically adds a `link_id` column to the child table with a `REFERENCES`
constraint on the parent.

### 4.2 Declaration

```python
from unisi import Table

customers = Table('Customers',
    id     = 'customers',
    fields = {'company': str, 'country': str},
)

# link = <parent table>  →  many-to-one (link_id FK)
orders = Table('Orders',
    id     = 'orders',
    fields = {'product': str, 'amount': float, 'status': str},
    link   = customers,   # ← just pass the parent table
)
```

> **Note:** On first run UNISI adds the `link_id` column via `ALTER TABLE`.
> Subsequent runs skip this step if the column already exists. This is
> separate from — and doesn't by itself guarantee — restart safety against
> Smart Schema Evolution's own comparison (see [§8](#8-schema-evolution)):
> that comparison runs *before* `setup_fk` gets a chance to add `link_id`,
> using only the `fields` the caller declared, which never include
> `link_id` since it isn't something you name yourself. UNISI accounts for
> this specifically (an on-disk `link_id` the declared `fields` doesn't
> mention is expected, not a real change) — worth knowing if you're
> ever comparing schemas yourself instead of going through `Table(...)`.

### 4.3 Behaviour in the UI

- Select a row in `Customers` — `Orders` automatically filters to rows linked to that customer.
- **`filter = True` (default):** Orders shows only rows with a matching `link_id`.
- **`filter = False`:** Orders shows all rows; matching ones are highlighted via `value`.

### 4.4 Adding a linked row from the UI

Click **append** in Orders while a customer is selected. UNISI:

1. Creates an empty row via `append_table_row`.
2. Automatically calls `dbtable.set_fk(new_row_id, master_id)` — stamps `link_id`.
3. The new row appears in the filtered Orders view.

### 4.5 Programmatic usage

```python
dbt = orders.rows.dbtable

# Add a row and immediately link it to customer ID=5
new_row = dbt.append_row({'product': 'Laptop', 'amount': 1200.0, 'status': 'new'})
order_id = new_row[-1]          # DB ID of the new row
dbt.set_fk(order_id, link_id=5) # link to customers.ID = 5

# Unlink (set link_id = NULL)
dbt.clear_fk(order_id)

# Fetch all orders of customers 3 and 5
linked = dbt.calc_linked_rows_fk(link_ids=[3, 5])
# With search:
linked = dbt.calc_linked_rows_fk(link_ids=[3, 5], search='Laptop')
```

### 4.6 Deleting a linked row

With `filter = True`, pressing **delete** on a selected row in Orders:

- Calls `dbtable.clear_fk(row_id)` — does **not** delete the row, only removes the link (`link_id → NULL`).
- The row disappears from the filtered view but remains in the database.

> **Warning:** To delete the row physically — switch to `filter = False` and
> delete from there, or call `dbtable.delete_row(id)` programmatically.

> **Seeding initial data?** `link = customers` only creates the `link_id`
> column — it links no rows. See [§5.8](#58-seeding-linked-data-at-startup)
> for the full recipe (written for many-to-many, but the same idea applies
> here with `set_fk` in place of `add_link`).

---

## 5. Many-to-Many (Junction Table)

### 5.1 Concept

Many-to-many uses an intermediate junction table (e.g. `products2tags`) that
stores `(src_id, tgt_id)` pairs. The relationship can carry extra payload fields
such as `weight` or `role`. Both sides are independent persistent tables.

### 5.2 Junction table schema

UNISI creates this automatically:

```sql
CREATE TABLE [products2tags] (
    src_id  INTEGER REFERENCES [products](ID) ON DELETE CASCADE,
    tgt_id  INTEGER REFERENCES [tags](ID)     ON DELETE CASCADE,
    -- optional payload fields:
    weight  REAL,
    ID      INTEGER PRIMARY KEY AUTOINCREMENT
)
```

`ON DELETE CASCADE`: deleting a row from `products` or `tags` automatically
removes the corresponding junction rows.

### 5.3 Declaration

**Without payload fields**

```python
tags = Table('Tags',
    id     = 'tags',
    fields = {'tag': str},
)

# link = [parent, {}]  →  many-to-many, no payload
products = Table('Products',
    id     = 'products',
    fields = {'name': str, 'price': float},
    link   = [tags, {}],
)
```

**With payload fields**

```python
products = Table('Products',
    id     = 'products',
    fields = {'name': str, 'price': float},
    # link = [parent, payload_fields, junction_name (optional)]
    link   = [tags, {'weight': float, 'note': str}],
    # junction table: products2tags with columns weight (REAL), note (TEXT)
    # explicit name: link = [tags, {'weight': float}, 'my_junction_name']
)
```

> **Note:** The default junction table name is `{src}2{tgt}`, i.e. `products2tags`.
> A custom name can be supplied as the third element of the `link` list.

### 5.4 Behaviour in the UI

- Select a tag in `Tags` — `Products` shows items linked to that tag.
- **`filter = True`:** Products filters by the junction table. Payload columns appear in the header prefixed with `Ⓡ` (e.g. `Ⓡweight`).
- **`filter = False`:** Products shows all items; linked ones are highlighted.
- The filter toggle sends `{event: "filter", value: true/false}`.

### 5.5 Adding a relationship from the UI

**`filter = True` — explicit row append**

Click **append** in Products while a tag is selected. UNISI:

1. Creates an empty row in `products` via `append_row`.
2. Inserts a junction record into `products2tags` via `dbtable.add_link(new_id, "tags", master_id)`.

**`filter = False` — checkbox selection**

Select/deselect rows in Products while a tag is selected. UNISI:

- Inserts junction records for newly selected rows (`dbtable.add_links(...)`).
- Deletes junction records for deselected rows (`dbtable.delete_links(...)`).

### 5.6 Programmatic usage

```python
dbt = products.rows.dbtable

# Add a single link (product_id=10 ↔ tag_id=3)
relation = dbt.add_link(snode_id=10, link_table='tags', tnode_id=3)
# With payload fields:
relation = dbt.add_link(10, 'tags', 3, link_fields={'weight': 0.8, 'note': 'featured'})

# Link multiple products to one tag
dbt.add_links(link_table='tags', snode_ids=[10, 11, 12], tnode_id=3)

# Delete junction rows by their IDs
dbt.delete_links('tags', link_ids=[55, 56])

# Delete links between specific src and tgt
dbt.delete_links('tags', source_ids=[10, 11], link_node_id=3)

# Fetch products linked to tags 3 and 7
linked = dbt.calc_linked_rows(
    index_name   = 'products2tags',  # junction table
    link_ids     = [3, 7],           # IDs in tags
    target_table = 'tags',
    include_rels = True,             # append junction columns to each row
    search       = 'Laptop',         # optional substring filter
)
```

### 5.7 Deleting a relationship from the UI

With `filter = True`, pressing **delete** on a selected row in Products:

- Deletes the `products2tags` record (the junction row) via `delete_links(..., link_ids=...)`.
- The product row in `products` is **not** removed — only the link to the tag is deleted.

> **Warning:** `delete` in `filter = True` removes only the relationship, not the
> object itself. For physical deletion use `filter = False` + delete, or call
> `dbtable.delete_row()` programmatically.

### 5.8 Seeding linked data at startup

`link = [tags, {...}]` (or `link = tags` for many-to-one) only creates the
*junction table* or the `link_id` *column* — the structure a relationship
would live in. It does **not** create any relationship. A table built like
this:

```python
products = Table('Products', id='products', fields={'name': str, 'price': float},
    rows = [['Widget', 9.99], ['Gadget', 19.99]],
    link = [tags, {'weight': float}],
)
```

seeds two independent `products` rows and whatever `rows=` gave `tags`, but
links **neither** to the other — selecting any tag shows zero products until
something actually calls `add_link`/`add_links`/`set_fk`. This is easy to
miss because nothing errors: the tables just render, empty of relationships.
`test_apps/db/screens/linked.py` demonstrates the fix end to end; the recipe
below is the general form.

**1. Fetch real rows, not `table.rows`.** By the time `Table(...)` returns,
a *child* table with `filter = True` (the default whenever `link=` is
given) has already had its `.rows` replaced by the current — still
empty, since nothing is selected yet — filtered view (§5.4/§4.3). The
`rows=` you just inserted are still in the database, just not reachable
through `.rows` anymore. Read them back through the `Dbtable` instead:

```python
odbt = products.rows.dbtable
all_products = odbt.read_rows(limit=odbt.length)   # unfiltered, with IDs
```

**2. Link specific rows.** Many-to-many: `add_link`/`add_links` against the
junction name the table itself computed (`table.rows.link[2]`, the third
element of the `(link_table, payload_fields, junction_name)` tuple `link=`
resolves to) rather than re-deriving or hard-coding it:

```python
rel_name = products.rows.link[2]         # e.g. 'products2tags'
odbt.add_link(all_products[0][-1], tags.id, some_tag_id,
              link_index_name=rel_name, link_fields={'weight': 0.8})
```

Many-to-one is simpler — no junction, no payload, just the FK:

```python
odbt.set_fk(all_products[0][-1], some_customer_id)
```

**3. Guard against reseeding.** A screen module's top-level code runs again
for *every new session* (each user gets their own screen instance — see
`unisi/modules.py`), so unconditional `add_link` calls insert another copy
of the same demo links on every single visit. `rows=` itself doesn't have
this problem — `Table.__init__` only inserts them the first time a table
with that `id` is ever created (`db.py`'s `get_table` reuses the existing
SQLite table on every later call) — but nothing does that bookkeeping for
you for links, since they're a separate, explicit step. Check whether the
junction (or the FK column) is already populated before seeding:

```python
if not Unishare.db.qlist(f'SELECT 1 FROM [{rel_name}] LIMIT 1'):
    for product, tag_id in zip(all_products, seed_tag_ids):
        odbt.add_link(product[-1], tags.id, tag_id, link_index_name=rel_name)
```

For many-to-one, check the FK column on the child table itself instead
(e.g. `Unishare.db.qlist(f'SELECT 1 FROM [{odbt.id}] WHERE link_id IS NOT NULL LIMIT 1')`).

**Putting it together** (many-to-many, matching `test_apps/db`'s screen):

```python
# screens/shop.py
tags = Table('Tags', id='tags', fields={'tag': str}, rows=[['Sale'], ['New']])
products = Table('Products', id='products', fields={'name': str, 'price': float},
    rows=[['Widget', 9.99], ['Gadget', 19.99]],
    link=[tags, {'weight': float}],
)

odbt = products.rows.dbtable
rel_name = products.rows.link[2]
if not Unishare.db.qlist(f'SELECT 1 FROM [{rel_name}] LIMIT 1'):
    all_products = odbt.read_rows(limit=odbt.length)
    all_tags = tags.rows.dbtable.read_rows(limit=tags.rows.dbtable.length)
    odbt.add_link(all_products[0][-1], tags.id, all_tags[0][-1],
                  link_index_name=rel_name, link_fields={'weight': 1.0})
```

Selecting **Sale** in `tags` now shows **Widget** in `products`, with no
extra step required from whoever opens the screen.

---

## 6. The `filter` Flag

The `filter` attribute (bool) controls how a linked table is displayed:

| `filter` | Display | `append` / `delete` behaviour |
|---|---|---|
| `True` (default) | Only rows linked to the selected master row | Creates / removes the link itself |
| `False` | All rows; linked ones are highlighted via `value` | In `editing` mode: manages links via checkboxes |

```python
# Toggle programmatically
orders.filter = False
link_table_selection_changed(...)   # redraw

# Client sends:
# { "block": "TBlock", "element": "Orders", "event": "filter", "value": false }
```

---

## 7. Displaying IDs and Relation Columns

### `ids` — show the ID column

By default the `ID` column is hidden. To display it:

```python
users = Table('Users',
    id     = 'users',
    fields = {'name': str, 'age': int},
    ids    = True,   # adds an "ID" column to headers
)
```

### Header markers

| Marker | Meaning | When it appears |
|---|---|---|
| `Ⓡ weight` | Junction table payload field | `filter=True` + many-to-many + payload fields |
| `✘ID` | Hidden ID (not editable) | `filter=True`, `ids=False` (default) |
| `ID` | Visible ID | `ids=True` |

---

## 8. Schema Evolution

On startup UNISI compares the declared `fields` against the real SQLite schema.
If a mismatch is detected, **Smart Schema Evolution** kicks in:

| Situation | Action |
|---|---|
| Schema matches | Table is used as-is |
| New columns added | Offers to `ALTER TABLE ADD COLUMN` |
| Columns removed | Offers to recreate the table without those columns |
| Type changed | Recreates the table and migrates data; backup is kept |

> **Note:** A backup is created automatically with the suffix `_backup_YYYYMMDD`.
> No original data is lost.

> **Restart safety:** "Schema matches" accounts for two kinds of columns
> that exist on disk but are never spelled out in a `Table(...)`'s own
> `fields=` — a `link_id` FK column (added by `setup_fk`, see
> [§4.2](#42-declaration)) and a POINT field's physical `_x`/`_y` pair
> (see [§11](#11-geo-spatial-fields)). Neither makes "schema matches" fail
> just because the caller's `fields` dict doesn't mention them by those
> exact names — otherwise every restart of an app with a linked or
> geo-tagged table would hit the interactive migration prompt below for a
> schema that, from the caller's point of view, never actually changed.

---

## 9. Full Example — Authors › Books › Genres

Three tables: authors (standalone), books (many-to-one → authors),
genres (standalone), and a many-to-many between books and genres.

```python
# screens/library.py
from unisi import Table, Screen

# ── Table 1: Authors (standalone) ────────────────────────────────────
authors = Table('Authors',
    id     = 'authors',
    fields = {'name': str, 'born': int},
)

# ── Table 2: Books (many-to-one → Authors) ───────────────────────────
books = Table('Books',
    id     = 'books',
    fields = {'title': str, 'year': int, 'rating': float},
    link   = authors,        # FK link_id → authors.ID
)

# ── Table 3: Genres (standalone) ─────────────────────────────────────
genres = Table('Genres',
    id     = 'genres',
    fields = {'genre': str},
)

# ── Table 4: Book–Genre links (many-to-many) ─────────────────────────
book_genres = Table('Book Genres',
    id     = 'book_genres',
    fields = {'title': str, 'year': int, 'rating': float},
    link   = [genres, {}],   # junction: book_genres2genres
)

screen = Screen(authors, books, genres, book_genres)
```

### Programmatic walkthrough

> The walkthrough below is written as a one-off script/REPL session. If
> this same code is going to live at the top of a **screen module**
> instead (so the links exist as soon as the screen first loads, with no
> manual step), see [§5.8](#58-seeding-linked-data-at-startup) first — that
> code re-runs on every new session, so it needs an idempotency guard this
> walkthrough doesn't.

```python
# Add an author
a_dbt  = authors.rows.dbtable
author = a_dbt.append_row({'name': 'Tolstoy', 'born': 1828})
author_id = author[-1]

# Add a book and link it to the author
b_dbt = books.rows.dbtable
book  = b_dbt.append_row({'title': 'War and Peace', 'year': 1869, 'rating': 9.8})
book_id = book[-1]
b_dbt.set_fk(book_id, author_id)

# Add genres
g_dbt = genres.rows.dbtable
g1 = g_dbt.append_row({'genre': 'Novel'})
g2 = g_dbt.append_row({'genre': 'Historical fiction'})

# Link the book to both genres
bg_dbt = book_genres.rows.dbtable
bg_dbt.add_link(book_id, 'genres', g1[-1])
bg_dbt.add_link(book_id, 'genres', g2[-1])

# Find all books in the "Novel" genre
linked = bg_dbt.calc_linked_rows('book_genres2genres', [g1[-1]], 'genres')
print([row[0] for row in linked.cache])   # ['War and Peace']
```

---

## 10. Dbtable API Reference

### Getting dbtable

```python
dbt = my_table.rows.dbtable   # from any persistent Table
```

### Read

| Method | Description |
|---|---|
| `dbt.list` | `Dblist` with the current page of rows (up to `limit`). The main table view. |
| `dbt.length` | Total row count in the DB (not just the in-memory page). |
| `dbt.read_rows(skip, limit)` | Read a range of rows directly from SQLite. |
| `dbt.search_rows(search)` | Return a `Dblist` of matching rows (`LIKE` across all text/numeric columns). |
| `dbt.init_list()` | Re-read the first page from the DB into `dbt.list`. |

### Single-Row Access

Reads here always go straight to SQLite — never through `dbt.list`'s page
cache. That cache is a *view*, kept in sync with the database by bumping
an internal version counter whenever a row is added, removed, or (as of
this section's methods existing) updated — but only through the methods
that know to bump it. Writing to a row by some *other* route entirely
(another process, direct SQL, …) and then immediately reading it back
through `get`/`find_one` is always safe; reading it back through `dbt.list`
is only as fresh as the last operation on *this* `Dbtable` that touched
the counter.

| Method | Description |
|---|---|
| `dbt.get(row_id)` | One row by ID, as `{field: value, ..., 'id': ...}`, or `None`. |
| `dbt.find_one(**field_equals)` | First row matching exact field values (AND-combined), or `None`. Values are bound query parameters, not string-interpolated. |
| `dbt.update(row_id, fields)` | Patch the given fields by ID; returns the fresh row, or `None` if the ID doesn't exist. |
| `dbt.row_to_dict(row, extra_fields=())` | Label a raw `[*fields, ID]` row (the shape every read method above, and `search_within_radius`/`search_nearest` below, actually return) into the same `{field: value, ..., 'id': ...}` shape `get`/`find_one` use. `extra_fields` names any columns appended after `ID` — e.g. `('distance_km',)` for a geo-search result. |

```python
user = dbt.get(42)
user = dbt.find_one(email='alice@example.com')
user = dbt.update(42, {'active': False})

for row in dbt.search_within_radius('location', lat, lng, 10):
    place = dbt.row_to_dict(row, extra_fields=('distance_km',))
```

> **Why not just use `search_rows`?** `search_rows` is a case-insensitive
> **substring** match across every text/numeric column — built for a GUI
> search box, where "close enough" is the point. `find_one(field=value)`
> is an exact match on the field(s) you name, which is what "does a row
> with exactly this key exist" actually needs — a `capability` value of
> `'repair'` would substring-match `'computer_repair'` too under
> `search_rows`, silently returning the wrong row.

### Geo-Spatial (see [§11](#11-geo-spatial-fields))

| Method | Description |
|---|---|
| `dbt.search_within_radius(field, lat, lng, radius_km, limit, where, params)` | Rows within `radius_km` of `(lat, lng)`, nearest first. Each row gets a trailing `distance_km`. |
| `dbt.search_nearest(field, lat, lng, k, initial_radius_km, max_radius_km, where, params)` | The `k` closest rows to `(lat, lng)` (expanding-radius search). |
| `haversine_km(lat1, lng1, lat2, lng2)` | Module-level great-circle distance in km; also registered as a SQL function of the same name. |

### Write

| Method | Description |
|---|---|
| `dbt.append_row(row)` | Insert one row (`list` or `dict`). Returns the stored row with its ID. |
| `dbt.append_rows(rows)` | Atomic bulk insert. Returns the list of stored rows with IDs. |
| `dbt.assign_row(row)` | Update a row by its ID (last element of the list). |
| `dbt.delete_row(row_id)` | Delete a row by its DB ID. |
| `dbt.delete_rows(ids)` | Delete multiple rows by a list of DB IDs. |
| `dbt.clear()` | Delete all rows. |

### Many-to-One (FK)

| Method | Description |
|---|---|
| `dbt.setup_fk(tname)` | Create the `link_id` FK column (called automatically). |
| `dbt.set_fk(row_id, link_id)` | Set `link_id` for a row. |
| `dbt.clear_fk(row_id)` | Clear `link_id` (NULL) for a row. |
| `dbt.calc_linked_rows_fk(link_ids, search="")` | Return rows `WHERE link_id IN (...)` with optional search filter. |

### Many-to-Many (junction)

| Method | Description |
|---|---|
| `dbt.setup_junction(tname, fields, relname)` | Create the junction table (called automatically). |
| `dbt.add_link(src_id, link_table, tgt_id, link_fields, index_name)` | Insert one record into the junction table. |
| `dbt.add_links(link_table, snode_ids, tnode_id)` | Insert multiple records (one `tgt` → many `src`). |
| `dbt.delete_link(link_table_id, link_id)` | Delete a junction record by its own ID. |
| `dbt.delete_links(link_table_id, link_node_id, source_ids, link_ids)` | Delete junction records matching a condition. |
| `dbt.calc_linked_rows(index_name, link_ids, target_table, include_rels, search)` | `JOIN` query through the junction table. Returns a `Dblist`. |

---

## 11. Geo-Spatial Fields

A field declared as a 2-element list or tuple of `float` (or `int`) — the
*types*, not coordinate values — is auto-detected as a geo-spatial point:

```python
Table('Offers', fields={
    'title':    str,
    'position': [float, float],   # or (float, float) — same thing
    'price':    float,
    'status':   str,
})
```

**Convention: `x` = longitude, `y` = latitude** — i.e. `position = [lng, lat]`,
matching the standard GIS/PostGIS/GeoJSON point order (`POINT(x y)` ==
`POINT(lon, lat)`). This is easy to get backwards if you're used to writing
"lat, lng" by habit, so it's worth committing to memory:

```python
bangkok = [100.5018, 13.7563]   # [lng, lat] — NOT [lat, lng]
```

### 11.1 What it looks like from the outside

A POINT field is still exactly **one** column everywhere you interact with a
table — one entry in `fields`, one header, one cell per row — holding a
`[x, y]` list (or `None`):

```python
offers.append_row({'title': 'AC repair', 'position': [100.5018, 13.7563],
                    'price': 1500, 'status': 'ACTIVE'})
# -> ['AC repair', [100.5018, 13.7563], 1500, 'ACTIVE', 1]

offers.list[0]                       # -> same shape, read back from SQLite
offers.list.update_cell(0, 1, [100.51, 13.76])   # move it — plain cell edit
```

### 11.2 What it looks like underneath

Internally, `position` becomes two ordinary `REAL` columns, `position_x` and
`position_y`, plus a composite index on `(position_y, position_x)`:

```sql
CREATE TABLE Offers (
    title      TEXT,
    position_x REAL_X,   -- REAL affinity; the distinct type name is what
    position_y REAL_Y,   -- lets UNISI fold the pair back into one logical
                          -- "position" field when it re-reads the schema
    price      REAL,
    status     TEXT,
    ID INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX Offers_position_geo_idx ON Offers (position_y, position_x);
```

You never write this SQL yourself — `create_table`/Smart Schema Evolution
generate and maintain it — but it's useful to know it's there, e.g. if you
ever inspect the `.db` file with an external SQLite tool.

### 11.3 Radius search

```python
nearby = offers.search_within_radius(
    'position',           # which POINT field
    13.7563, 100.5018,    # lat, lng of the search origin
    5.0,                  # radius_km
    where='status = ? AND price <= ?',
    params=('ACTIVE', 2000),
)
for title, position, price, status, row_id, distance_km in nearby:
    print(f"{title}: {distance_km:.2f} km, {price}")
```

Rows come back **nearest first**. Each row is the normal
`[...fields..., ID]` shape with one extra value, `distance_km`, appended
right after the ID — so it's one element longer than a row from
`read_rows()`/`append_row()`. `where`/`params` add an extra,
already-parameterised `AND` condition, so a single call can combine the geo
filter with ordinary business filters — capability, price, status — exactly
like the reference query this feature was modelled on.

### 11.4 Nearest-k search

```python
closest_five = offers.search_nearest('position', 13.7563, 100.5018, k=5)
```

This is `search_within_radius` run with an expanding radius (starting at
`initial_radius_km=5.0`, doubling up to `max_radius_km=500.0` by default)
until at least `k` rows are found. It may return **fewer than `k`** rows if
the table has fewer than `k` matches within `max_radius_km` — that cap
exists so a sparse area can't silently turn into an unbounded full-table
scan; raise `max_radius_km` (or lower it, for a tighter cost ceiling) to
change that trade-off.

### 11.5 Raw SQL

The same `haversine_km(lat1, lng1, lat2, lng2)` function `search_within_radius`
uses is registered on every connection, so it's usable directly:

```python
rows = db.qlist(
    "SELECT title, haversine_km(position_y, position_x, ?, ?) AS km "
    "FROM Offers WHERE km <= ? ORDER BY km",
    (13.7563, 100.5018, 5.0),
)
```

(Note the raw-SQL column names are the physical `position_x`/`position_y`,
not the logical `position` — that folding only happens at the Python
`Dbtable` layer.)

### 11.6 Why bounding-box + haversine

Three ways to do geo-matching in SQLite were on the table; this is the one
UNISI uses, and why:

| | Bounding-box + `haversine()` (chosen) | R\*Tree virtual table | SpatiaLite |
|---|---|---|---|
| Dependencies | none — Python 3.10+ stdlib only | SQLite compile-time option; not guaranteed on every build | external `mod_spatialite` binary — rarely preinstalled |
| Setup complexity | one index, one registered function | a synced shadow table (triggers or app-level sync on every write) | extension loading, platform-specific binary |
| Measured performance | **~1 ms at 50,000 rows** (bounding-box pre-filter narrows the candidate set before `haversine_km()` ever runs) | faster at very large scale (100k+) | comparable to R\*Tree once set up |
| Best for | the range a single SQLite file is normally used for | tables that outgrow the above | full OpenGIS surface (polygons, `ST_Within`, buffers) |

A synced R\*Tree shadow table is a reasonable next step if a single table's
POINT-filtered queries genuinely need to scale past what a plain B-Tree
range scan handles well — but it adds a second structure that has to stay
correct on every insert/update/delete, which is real ongoing complexity for
most apps' actual data volumes. Start here; reach for R\*Tree only once
you've measured that you need it.

### 11.7 Known limitations

- **Antimeridian / poles**: the bounding box is a locally-flat
  (equirectangular) approximation. It's exact enough for any reasonable
  radius at ordinary latitudes, but searches very close to the ±180°
  longitude line or within a few degrees of the poles can miss matches on
  the "other side" of the wraparound.
- **Search excludes POINT columns**: `search_rows`/the `search` UI event
  never match against coordinates (see [§3.7](#37-search)) — use
  `search_within_radius`/`search_nearest` for location queries.
- **Junction (many-to-many) payload fields** can be POINT-typed too
  (`setup_junction('Zones', {'meeting_point': [float, float]})`) and round-trip
  correctly through `add_link`/`calc_linked_rows(..., include_rels=True)` —
  but there's no `search_within_radius` equivalent for a junction table's own
  payload; query it with raw SQL (§11.5) if you need that.
