# UNISI Framework Programming Documentation and Specification

This document is a programmer-focused specification for building applications with the `unisi` framework.  

## 1. Purpose and Scope

UNISI provides:
- automatic web GUI rendering from Python objects
- synchronized client/server state
- event-driven handlers (sync and async)
- optional services: hot reload, autotest, DB-backed tables, LLM-assisted fields, API handlers, persistent unit/screen state

UNISI targets Python `3.10+`.

## 2. Minimal Project Structure

At runtime, UNISI expects:
- `config.py` in working directory (auto-created with defaults if absent)
- `screens/` folder with one or more `*.py` screen modules
- optional `blocks/` folder for shared reusable blocks

Typical startup script:

```python
import unisi
unisi.start()
```

## 3. Runtime Configuration (`config.py`)

Supported keys (from defaults in `unisi/utils.py`):

| Key | Type | Default | Meaning |
|---|---|---|---|
| `port` | int | `8000` | HTTP/WebSocket server port |
| `appname` | str | `"Unisi app"` | Default app header |
| `upload_dir` | str | `"web"` | Upload/static exposed dir |
| `hot_reload` | bool | `False` | Reload code changes |
| `autotest` | bool/str/list | `False` | Autotest mode: `True`/`'*'` runs every recorded fixture, a list restricts to named files |
| `logfile` | str/None | `None` | Optional log file |
| `mirror` | bool | `False` | New anonymous connections reflect the most recent user's session |
| `share` | bool | `False` | Session reattachment via `?session=` |
| `profile` | int/float | `0` | Log a warning if a handler runs longer than this many seconds |
| `froze_time` | int/float/None | `None` | Log a warning if a session waits longer than this many seconds (monitoring) |
| `monitor_tick` | float | `0.005` | Poll interval, in seconds, for the monitor process and `run_process` |
| `pool` | int/None | `None` | Size of the `multiprocessing.Pool` used by `user.run_process()`; `None` = `os.cpu_count()` |
| `debug` | bool | `False` | Debug flag read by the runtime |
| `llm` | tuple/list/None | `None` | LLM provider config |
| `llm_cache` | str (optional) | unset | Directory enabling a persistent disk cache for `Q()`/`Qx()` results |
| `llm_cache_ttl` | int/None | `None` | Cache entry lifetime in seconds; `None` = never expires |
| `temperature` | float | `0.0` | LLM sampling temperature |
| `strict_schema` | bool | `True` | Whether structured `Q()` calls request strict JSON-Schema enforcement |
| `reasoning` | str (optional) | unset | Effort level (e.g. `'medium'`) forwarded as `extra_body.reasoning` for reasoning models |
| `db_path` | str/None | `None` | DB file path for persistent tables (or set `UNISI_DB_PATH` env var) |
| `lang` | str | `"en-US"` | UI language |
| `public_dirs` | list[str] | `[]` | Extra static roots |
| `web_client` | str/None | `None` | Root dir of a custom UNISI-protocol web client to serve at `/` instead of the bundled one (still reachable at `/default`) — see §17 |
| `image` | str | `"icons/favicon-32x32.png"` | App icon |
| `session` | str/None | None | optional session/user id for debugging |
| `persist` | bool | `False` | Persist every unit on every screen, globally |

## 4. Programming Model

Core entities:
- `Screen`: top-level UI page
- `Block`: container of UI units
- `Unit`: interactive widget (`Button`, `Edit`, `Table`, etc.)
- `User`: runtime session object bound to current client

Event flow:
1. User triggers event in browser.
2. UNISI locates target element (`block`, `element`, `event`).
3. UNISI executes interception handler (if registered via `@handle`).
4. UNISI executes element handler (sync or async).
5. Return value is converted to UI updates/messages.

## 5. Screen Specification

Each file in `screens/` is loaded as a screen module.

Required globals:
- `name: str`
- `blocks: Block | list`

Optional globals:
- `order: int`
- `icon: str`
- `header: str`
- `toolbar: list[Unit]`
- `prepare: callable`

Always injected by runtime in screen module:
- `user` (current `User`)
- `screen` (current `Screen`)

Example:

```python
from unisi import *

name = "Main"
order = 0

counter = Edit("Counter", 0)
btn = Button("Inc", lambda *_: counter.accept(counter.value + 1) or counter)
blocks = [Block("Demo", [btn], counter)]
```

## 6. Block Specification

Constructor:

```python
Block(name, *children, **options)
```

Important options:
- `width`
- `scroll`
- `scaler`
- `icon`
- `closable`

Layout rules:
- plain sequence in `blocks` -> default orientation layout
- nested lists define sub-layout areas
- list of units inside a block row renders inline

Example from tests:

```python
blocks = [[block_a, block_b], config_area]
```

## 7. ParamBlock Specification

Constructor:

```python
ParamBlock(name, *units, changed=None, row=3, strict='recurse', persist=False, **params)
```

- `changed`: shared handler used as the `changed` callback for every field generated from `params` (same effect as passing it to each field individually).
- `row`: number of fields per visual row.
- `strict`: `'recurse'` (default) turns a nested `dict` value into an embedded `ParamBlock`; any other truthy value raises on an unsupported value type; falsy silently skips unsupported values instead of raising.
- `persist`: same contract as on any `Unit`/`Block` — `True` for positional persist, or a key-function for keyed persist (§13.2). Because `ParamBlock`'s fields are generated from `params`, keyed persist saves/restores the **whole `params` dict**, not individual fields.

Parameter mapping (value type -> generated widget):
- `bool` -> `Switch`
- `str` / `int` / `float` -> `Edit`
- `(value, options)` where `options` is a 3-item list of numbers `[min, max, step]` -> `Range`
- `(value, options)` where `options` is any other list/tuple -> `Select`
- `(value, options)` where `options` is a `dict` -> `Tree`
- `dict` (only when `strict='recurse'`) -> embedded `ParamBlock`

Read current values:

```python
params = param_block.params
```

`params` is also **writable**. Assigning a new dict fully rebuilds the block's fields to match it: fields absent from the new dict are dropped, new keys create new fields with the widget types above. This is the supported way to repoint a `ParamBlock` at a different record:

```python
param_block.params = load_settings_for(selected_row.id)
```

Reassigning `params` after the screen has already been built and displayed is fully supported — the new fields are wired up with reactivity and tree position exactly like fields created at screen-build time, so edits to them are tracked normally.

Example:

```python
block = ParamBlock(
    "System parameters",
    per_device_eval_batch_size=16,
    warmup_ratio=0.1,
    logging_steps=(10, [1, 20, 1]),
    device=("gpu", ["cpu", "gpu"]),
    load_best=True,
)
```

Example with keyed persist (remember manual overrides per selected record — see §13.2):

```python
selected = Select("Record", options=["A", "B", "C"])
settings = ParamBlock(
    "Settings",
    persist=lambda: (selected.value,),
    Threshold=5.0,
    Enabled=True,
)
```

## 8. Event and Handler Specification

Handler signatures:

```python
def handler(unit, value): ...
async def handler(unit, value): ...
```

Return contract:
- `None`: accept and sync current state
- `Unit` or list of `Unit`: explicit updates
- `Info(...)`, `Warning(...)`, `Error(...)`: show user message
- `Dialog(...)`: open dialog
- `True` or `Redesign`: screen-level update behavior

Common helper methods:
- `unit.accept(value)` for standard value assignment path
- `user.set_screen("Screen Name")` for navigation
- `await user.progress("text")` for progress UI

## 9. Event Interception (`@handle`)

Use `handle(unit, event)` to intercept/extend behavior (especially shared blocks).

```python
from unisi import handle, Warning

@handle(selector, "changed")
def reject_based(unit, value):
    if value == "Based":
        return Warning("Mode cannot be Based", unit)
    return unit.accept(value)
```

Interception is registered in global handler map and executed before/default instead of element-local event logic.

> `@handle`/`Unishare.handle` targets whichever `User` is "current"
> (`User.last_user`) at registration time — the normal case, since a
> screen's handlers register while that screen is being compiled for a
> specific session. A persistent `Table` declared at plain module level
> (outside any screen, so it's constructed once and shared — see §12)
> registers its own `search`/`filter`/`changed` handlers the same way, at
> plain `import` time, before `unisi.start()` has created anyone: there is
> no "current" `User` yet. That case is handled, not an error — the
> registration is held and every subsequently-constructed `User` starts
> with it already present, so the shared table's handlers still reach
> every real session correctly.

## 10. Dialog Specification

Constructor:

```python
Dialog(question, callback, *content, commands=["Ok", "Cancel"], icon="not_listed_location")
```

Callback receives pressed command button name:

```python
async def on_dialog(dialog, command):
    if command == "Ok":
        ...
```

## 11. Unit Catalog (Practical API)

From `unisi/units.py`:

Every unit below also accepts a common `persist` kwarg (positional `True` or a key-function) to opt into state persistence — see §13.

- `Button(name, handler=None, **kwargs)`
- `Edit(name, value?, changed?, **kwargs)`
- `Text(name, ...)` (read-only label style)
- `TextArea(name, value?, changed?, **kwargs)`
- `Range(name, value?, changed?, options=[min,max,step])`
- `Switch(name, value=False, changed?)`
- `Select(name, value?, options=[])`
- `Tree(name, value?, options=dict|list)`
- `Chart(name, option, changed?)`
- `HTML(name, html_string, changed?, scale?, edit?)`
- `Image(name_or_url, value=False, handler=None, label="", width=300, ...)`
- `Video(name, value = { "position": float, "play": bool, "volume": Number},
     changed?, fragments=[{url: str, start: float, end: float}])
- `Sound(name, value = {'url': str, "position": float, "play": bool, "volume": Number},
- `Graph(name, value?, changed?, nodes=[], edges=[])`
- `Net(name, ...)` (graph of screen/block/unit topology)

Name convention:
- prefix `_` in unit name hides visible label in UI.

## 12. Table Specification

Constructor pattern:

```python
Table(name, value?, changed?, **kwargs)
```

Common table options:
- `headers`
- `rows`
- `type="table"` or `type="chart"`
- `view="i-1,2"` for chart projection
- `multimode=True` for multi-row select
- `append`, `modify`, `delete`, `complete`, `update` handlers

Pandas mode:

```python
Table("Zoo Table", panda=df)
```

Persistent DB mode (requires `config.db_path` or `UNISI_DB_PATH` env var):
- provide `id` and `fields` or compatible DB schema
- supports `ids`, `filter`, `search`, linking
- safe to declare at plain module level (e.g. a `data_model.py` imported
  by both a screen and plain backend code such as an HTTP handler), not
  only inside a screen's own compilation — construction does not depend
  on a `User` already existing (see §9)
- restart-safe: redeclaring the same `fields`/`link=` against an existing
  database does not re-trigger Schema Evolution just because the FK
  column (`link_id`) isn't spelled out in `fields` — see
  `docs/persistent_tables.md` §8 for the underlying reason
- single-row access from backend code (webhook handlers, jobs, …) that
  only knows a row's ID or an exact field value: `dbtable.get(row_id)`,
  `dbtable.find_one(**field_equals)`, `dbtable.update(row_id, fields)` —
  see `docs/persistent_tables.md` §3.4/§10 for the full reference; reads
  always go straight to SQLite, never through `dbt.list`'s page cache

Linked tables — `link=` takes one of two shapes, and the *shape* alone
decides many-to-one vs many-to-many (an empty relation-fields dict does
**not** make it many-to-one — see `docs/persistent_tables.md` §4–§5):

```python
# many-to-one: link = <parent table>, bare, no list/tuple around it
# -> FK column `link_id` added to this table, no junction table
otable = Table(
    "Orders",
    id="Orders",
    fields={"name": str, "sum": float},
    link=utable,
)

# many-to-many: link = [<parent table>, <relation fields dict>] (list or
# tuple; an optional 3rd element names the junction table explicitly —
# omitted, it defaults to "<this id>2<parent id>")
# -> junction table, even when the dict is {}
otable = Table(
    "Orders",
    id="Orders",
    rows=orders,
    headers=["name", "sum"],
    link=(utable, {"type": "string", "weight": "double"}),
)
```

See `docs/persistent_tables.md` §4 (many-to-one) and §5 (many-to-many) for
the full read/write API each shape gets: `set_fk`/`clear_fk`/`calc_linked_rows_fk`
vs `add_link`/`delete_link`/`calc_linked_rows`.

> Table's persistent DB mode manages application data rows and is a separate system from Unit/Screen state persistence (`persist=...`, §13).

## 13. State Persistence Specification

Any `Screen`, `Block`, or `Unit` can opt into having its state survive across requests — and, for screens, be restored when the screen is next loaded — by setting `persist`. There are two distinct modes depending on what you pass.

### 13.1 Positional persist (`persist=True`)

The default mode. Set `persist=True` on:
- a screen module (module-level `persist = True`, or `screen.persist` in `prepare()`) — persists every unit on that screen;
- an individual `Block` or `Unit` — persists just that subtree.

Storage is **keyed by the unit's position** in the screen tree, scoped to the current user session. The position is a name chain read leaf first — the unit's own name, then each containing block going outward, ending with the screen — `'@'`-joined the same way `user.find_path(unit)` returns it (see `persist_location`, §13.4). Whenever a persisted unit changes, its current state is saved — precisely once, at the true end of the request that changed it, never at an intermediate `await user.progress(...)` tick or a dialog's own close notice along the way (those are out-of-band pushes to the client mid-request, not save points, however many of them a single handler triggers); when the screen is next loaded, the saved state is restored onto the unit at the same tree position. This is the default, screen-relative case; a block imported from `blocks/` and reused across screens anchors position and identity differently — see §13.6.

This is the right tool for "remember what this widget was last set to for this user" — a settings toggle, a filter's last value, a panel's last-expanded state.

It does **not** distinguish between different records shown through the same widget: if one `Edit` is reused to display different rows as the user navigates, positional persist only knows "this widget, this screen," not "this row." Use keyed persist for that case.

```python
volume = Range("Volume", 50, persist=True)   # remembered for this user on this screen
```

### 13.2 Keyed persist (`persist=<function>`)

Pass a zero-argument function instead of `True`. It must return a tuple (or list) of plain, JSON-serializable values — typically read from other units on the same screen — that together identify which record/context the unit currently reflects:

```python
selected_row = Select("Product", options=["Widget", "Gadget", "Gizmo"])
price = Edit("Price", 0.0, persist=lambda: (selected_row.value,))
```

The tuple becomes a `context_key` string — plain and readable, not JSON: one value is used as its own `str()`, several are joined with `,` — e.g. `("Widget",)` → `'Widget'`, `('London', 123)` → `'London,123'`. This is exactly what `get_objects`/`get_contexts`/`persist_location` show you and what a template (§13.4) is written against, so `'London,..'` finds every record for London without knowing any encoding scheme. A literal `,` or `\` *inside* one value is backslash-escaped so two different tuples can never collide onto the same string (`('a,b', 'c')` and `('a', 'b,c')` would otherwise both read `'a,b,c'`) — invisible in the common case where key values don't themselves contain a comma.

On every request, UNISI recomputes the key for each such unit:
- If the key **changed** since last checked, it looks up a saved value for the new key:
  - **found** — the unit's value (or, for a `ParamBlock`, its whole `params` dict — see §7) is replaced with the saved one, and `unit.active` is set to `True`.
  - **not found** — the unit is left as-is (whatever a `changed` handler or `llm` computation already put there), and `unit.active` is set to `False`.
- If the key is **unchanged** but the unit — or, for a block, anything inside it — was edited this request, its current value is saved under that key and `unit.active` is set to `True`.

`active` is an ordinary reactive property, readable and stylable on the client like any other — a natural way to indicate "this field holds a manual override for the current record" versus "showing the computed default."

Keyed persist is per-unit and independent of screen position, so it correctly handles a widget reused across many different records — exactly the case positional persist can't.

### 13.3 Simple key-value storage

For state not tied to any particular unit or screen, `User` exposes a flat get/set pair backed by the same storage:

```python
user.set_key("last_export_format", "pdf")
fmt = user.get_key("last_export_format")   # None if never set
```

`get_keys(template)` searches that same store by key prefix/suffix instead of an exact key, and returns every match as a `{key: value}` dict:

```python
user.set_key("export_2024", "pdf")
user.set_key("export_2025", "csv")
user.set_key("theme_dark", True)

user.get_keys("export_..")     # prefix  -> {"export_2024": "pdf", "export_2025": "csv"}
user.get_keys("..2025")        # suffix  -> {"export_2025": "csv"}
user.get_keys("export_..2025") # both    -> {"export_2025": "csv"}
```

`template` must contain the literal `..`, marking where arbitrary text may appear; the text before/after it (`ab`/`ba`) is matched verbatim at the start/end of the key. Returns `{}` if nothing matches (or nothing was ever stored yet). Raises `ValueError` if `template` doesn't contain `..`.

`remove_key`/`remove_keys` delete from the same store, mirroring `get_key`/`get_keys` exactly — same template rules, and each returns what it just deleted:

```python
old = user.remove_key("theme_dark")        # deletes it, returns True (the old value); None if it didn't exist
gone = user.remove_keys("export_..")       # deletes every match, returns {"export_2024": "pdf", "export_2025": "csv"}
```

### 13.4 General object search (`get_objects`)

`get_key`/`get_keys` only reach the simple store (`namespace=''`, `path=''`). `get_objects(namespace, path, context_template)` is the same kind of search generalized to any `(namespace, path)` — in particular the keyed-persist rows from §13.2, letting you list saved records for a unit's key function instead of looking up one key at a time:

```python
# unit "price" nested in block "form" on screen "orders" -- path is '@'-joined,
# leaf first (same order as User.find_path), so "price@form", persist=lambda: (selected_row.value,)
user.get_objects("orders", "price@form", "..")
# -> {'Widget': {...saved fields...}, 'Gadget': {...saved fields...}}
```

`context_template` behaves like `get_keys`'s template when it contains `..` (prefix/suffix match). Unlike `get_keys`, a template with no `..` is not an error — it's an exact `context_key` match instead, so `get_objects` also covers a single positional-persist lookup (`context_key=""`, §13.1):

```python
user.get_objects("orders", "price@form", 'Widget')      # exact match -> that one record, or {}
user.get_objects("settings_screen", "state@panel", "")  # exact "" -> the persist=True save, if any
```

Returns `{context_key: fields_dict}`, empty if nothing matches or the session has no DB yet. Read-only.

`get_contexts(namespace, path, context_template)` — same parameters, same exact-vs-template rule, but returns just the matching context_keys as a `list[str]` instead of a `{context_key: fields}` dict, without reading or decoding the stored fields at all. Cheaper than `get_objects` when you only need to know which records exist:

```python
user.get_contexts("orders", "price@form", "..")   # -> ['Widget', 'Gadget']
```

### 13.5 Storage and Scope

All of the above share the same storage: a local SQLite file per user session (`users/<session-id>.db`), created on first write. State is never shared between users or sessions. Persistence is automatically disabled during autotest runs.

### 13.6 Persistence and shared blocks

A unit living inside a block imported from `blocks/` (the same object embedded, by reference, in every screen that imports it — see §16) is not scoped to whichever screen currently displays it. Its storage identity is anchored to the block's own Python module instead: namespace is `'@' + <module's dotted path>` (e.g. `'@blocks.header'`) rather than a screen name, and its tree path is measured from the block's own root, not the screen. Its persisted state — single fields, whole-block state, keyed records — is therefore the same no matter which screen the user is currently on, and survives a restart even if the user's first screen this session isn't the one that originally saved it.

This applies automatically wherever `persist=True` / `persist=<function>` is set directly on the shared block or its fields. It does NOT automatically apply to a unit that is merely cascaded into persistence by an unrelated screen's module-level `persist = True` (§13.1): that cascade only takes effect once that screen has actually been loaded at least once in the current session. For a widget meant to be shared and persisted, prefer setting `persist=True` (or a key-function) directly on it or its containing block in `blocks/`, rather than relying on a hosting screen's blanket `persist = True`.

```python
# blocks/header.py
theme = Select("Theme", "light", options=["light", "dark"], persist=True)
header_block = Block("Header", theme)
```

`theme` persists under `('@blocks.header', 'Theme')` regardless of which screen imports `header_block`, including a screen that has never declared its own `persist = True` and is the very first one loaded this session. (A field that is *not* separately named at module level — created inline as one of `header_block`'s children instead — gets a path measured leaf first, from it up to `header_block`, e.g. `'Theme@Header'`; either way the anchor is the block's module, never the hosting screen.)

To look a specific unit's saved record up directly (e.g. via `get_objects`/`get_contexts`, §13.4) without hardcoding which namespacing scheme applies, use `user.persist_location(unit)`, which returns the `(namespace, path)` currently in effect for it.

### 13.7 On-demand save/restore (`persist_units` / `restore_units`)

For a unit you only want to snapshot or revert at one specific moment — e.g. a "Save"/"Revert" button — rather than on every change (positional, §13.1) or per selected record (keyed, §13.2), `User` exposes an explicit imperative pair:

```python
user.persist_units(*units, context_key=None)    # save each unit's current state right now
user.restore_units(*units, context_key=None)    # load each unit's last saved state and apply it
```

Both work on **any** `Unit`, or `Block`/`ParamBlock` (whole subtree at once), whether or not it carries `persist=...` at all — that is the point: a way to persist something that is otherwise not automatically persistent. With `context_key` left at its default `None` (treated exactly like `""`), they read/write the same `(namespace, path, context_key="")` row a positional `persist=True` on that unit would use (see `persist_location`, §13.4), so this is an eager, explicit trigger for that slot rather than a separate mechanism — a unit force-saved this way is exactly what a later screen load would restore automatically if `persist=True` were added to it, and calling `persist_units` on a unit that already has `persist=True` is simply an extra, redundant-but-harmless save of the same slot the automatic mechanism already maintains. A `Block`/`ParamBlock` passed in saves/restores its **whole subtree at once**, however deep — `persist_units` walks every nested `Unit` inside it into one JSON blob under the block's own path, and `restore_units` resolves every nested reference in that blob back onto its live counterpart by tree path — a grandchild three levels down is included exactly like a direct child.

```python
draft = TextArea("Draft", "")
toolbar = [
    Button("Save",   lambda *_: user.persist_units(draft)),
    Button("Revert", lambda *_: user.restore_units(draft)),
]
```

Passing a non-`None` `context_key` targets a different, explicitly-named row instead — `(namespace, path, context_key)` — so the same unit (leaf or whole `Block` subtree) can hold any number of independent on-demand snapshots side by side, without disturbing the default `""` slot or each other. It's a plain caller-chosen string, not template-matched the way a keyed-persist context_key can be (§13.2); pass the same string back to `restore_units` to load that particular snapshot, or use `get_objects`/`get_contexts` (§13.4) to enumerate what's been saved for a unit across all of its context_keys:

```python
# stash the current form as a named checkpoint before a risky bulk edit,
# independently of the form's own default persist_units()/persist=True slot
user.persist_units(form_block, context_key="before_bulk_edit")
...
user.restore_units(form_block, context_key="before_bulk_edit")
```

Behavior notes:
- `persist_units` skips (silently) a unit whose current state already matches what's stored under that same `context_key` — no redundant write — and returns only the units it actually wrote, in call order.
- `restore_units` skips (silently) a unit with nothing saved under that `context_key` yet, and returns only the units it actually found and applied, in call order.
- Either one skips a unit that isn't reachable from the current screen — the same condition under which `persist_location` returns `None` — and logs a warning for it, since (unlike the automatic mechanisms, which routinely skip units every request as a matter of course) an explicit call naming a specific unit is more likely a mistake worth surfacing.
- A unit already governed by keyed persist (`persist=<function>`, §13.2) has its own current-record slot maintained automatically every request by the keyed-persist mechanism; `persist_units`/`restore_units` target the unrelated positional slot on such a unit (default or explicitly-keyed via `context_key`), not that keyed slot — the two don't substitute for one another.
- Restoring a `Block` re-renders it wholesale on the client, same as any other direct mutation of a container (§13.2's rationale for why keyed persist avoids container targets applies here too) — prefer restoring individual leaf units if part of the block may be mid-edit on the client.

## 14. LLM Integration Specification

Two levels:
1. Unit/Table `llm` dependency auto-fill
2. Explicit async queries via `Q` and `Qx`

### 14.1 Unit and Table `llm`

Examples:

```python
ebirth = Edit("Date of birth", llm=True)              # infer from block context
occupation = Edit("Occupation", llm=ename)            # infer from one dependency
table = Table("Persons", llm={"Date of birth": "Name", "Occupation": True}, ...)
```

### 14.2 Explicit queries

`Q(prompt, type_value=..., images=None, **format_vars)` returns an awaitable with typed JSON validation.

```python
country_info = await Q(
    "Provide information about {country}.",
    dict(capital=str, population=int, currency=str),
)
```

`Qx(prompt, type_value=str, images=None)` is raw/non-extended prompt mode.

`images` (optional, on both `Q` and `Qx`) attaches one or more images to the query for vision-capable models — a single value or a `list`; `None` by default, which sends no image and leaves the request/cache key identical to a call made without this parameter at all. Each image is one of:
- `'http://...'` / `'https://...'` — passed straight through as a remote URL
- `'data:image/...;base64,...'` — passed straight through as-is
- any other `str` — a local file path, read and base64-encoded automatically
- `bytes` / `bytearray` — base64-encoded automatically, MIME type sniffed from content
- a `dict` for manual control — `{'url': ...}`, `{'path': ...}`, or `{'data': b'...', 'mime': '...'}`, optionally with `'detail': 'low'|'high'|'auto'` — or an already-built `{'type': 'image_url', 'image_url': {...}}` part, passed through unchanged

```python
caption = await Q("Describe this photo.", str, images="photo.jpg")
diff = await Q("What changed between these?", list[str], images=[url_before, url_after])
```

Not to be confused with the `Image` unit (§11), which displays a picture in the UI — `images` here sends a picture *to* the LLM as input. Whether the request succeeds still depends on the configured `config.llm` provider/model actually supporting image input; `Q`/`Qx` don't check that in advance, the provider's own error surfaces normally if it doesn't.

LLM provider is configured through `config.llm`.

## 15. HTTP Route Integration

You can add custom aiohttp routes while keeping UNISI runtime:

```python
from aiohttp import web
import unisi

async def handle_get(request):
    return web.Response(text=request.query_string)

unisi.start(http_handlers=[web.get("/get", handle_get)])
```

## 16. Shared Blocks and Reuse Pattern

Place reusable block modules in `blocks/` and import into screens:

```python
from blocks.tblock import config_area
blocks = [config_area]
```

Use interception (`@handle`) in screen module when you need screen-specific behavior overrides for shared units.

For how `persist` behaves on a unit living in a shared block — storage anchored to the block's own module rather than to whichever screen displays it — see §13.6.

## 17. Custom Web Client (`config.web_client`)

UNISI's HTTP layer (`static_serve` in `server.py`) can serve a separate front end in place of its own bundled Quasar-based client, as long as that front end speaks the UNISI protocol (opens a WebSocket to `/ws` and exchanges the same JSON messages `handle`/`websocket_handler` produce and consume — `config.web_client` only changes which static files answer `GET /`, never the protocol itself).

Activation:

```python
# config.py
web_client = 'custom_client/dist'  # relative (to cwd) or absolute path
```

Resolution order for any request path, inside `static_serve`:

1. `/default` or `/default/<anything>` — always the bundled client (`unisi/web`, exposed as `webpath`), regardless of `config.web_client`. This path segment is reserved; a custom client cannot serve its own content there.
2. Otherwise, the active root: `config.web_client` if set, else the bundled client (`active_webpath()`).
3. If a custom client is active and step 2 missed, the bundled client again, as a fallback (see below for why).
4. `config.public_dirs`, unchanged from the no-`web_client` case.
5. 404.

Steps 1–3 all apply the same traversal protection as the original webpath lookup (`resolve_in_root`): the resolved path must stay inside whichever root is being checked.

**Why the step-3 fallback exists.** The bundled client's production build hardcodes its webpack `publicPath` as `"/"` and references its own entry scripts with root-relative paths (`/js/<hash>.js`), not paths relative to wherever its HTML was actually served from. A `<base>` tag doesn't change that — it only affects relative URLs, not ones that already start with `/`. So even while the bundled client's `index.html` is being viewed through `/default`, the browser still requests its JS/CSS/icons from unprefixed root paths like `/js/<hash>.js`. Without step 3, those requests would 404 against a custom client's own root instead of resolving, and `/default` would render as an unstyled, non-functional shell whenever a different `web_client` is active. Step 3 is what keeps `/default` genuinely usable in that case; as a side effect it also lets a custom client omit files (favicon, fonts, ...) it's happy to inherit from the bundled one.

**Startup validation.** `start()` calls `warn_if_web_client_misconfigured()`, which prints a warning (not a hard failure) if `config.web_client` is set but has no `index.html` at its root. A misconfigured `web_client` degrades gracefully rather than breaking the app: every lookup against it simply misses, so `/` ends up served entirely by the bundled client via the step-3 fallback until the path is fixed.

Relevant names, all in `unisi/server.py` next to `static_serve()`: `DEFAULT_CLIENT_ROUTE`, `active_webpath()`, `resolve_in_root()`, `warn_if_web_client_misconfigured()`. Tests: `tests/core/test_web_client.py`.

## 18. End-to-End Example (Runnable Pattern)

```python
# run.py
import unisi
unisi.start()
```

```python
# screens/main.py
from unisi import *

name = "Main"

def validate_ratio(unit, value):
    if not (0.0 <= value <= 1.0):
        return Error("Ratio must be between 0 and 1", unit)
    return unit.accept(value)

ratio = Range("Ratio", 0.5, validate_ratio, options=[0.0, 1.0, 0.1])
log = TextArea("Log", "Ready")

async def run_task(*_):
    await user.progress("Working...")
    log.value = f"Ratio: {ratio.value}"
    return [log]

controls = Block("Controls", [Button("Run", run_task)], ratio, log, icon="api")
blocks = [controls]
```

## 19. Behavior Notes and Constraints

- Screen and block names should be unique in their active context.
- For DB-backed `Table`, `config.db_path` (or `UNISI_DB_PATH`) must be set; otherwise creation fails.
- If a handler is missing, `changed` events assign incoming value directly.
- Dialog remains active if callback returns message/update that keeps it open.
- `prepare()` runs when screen is displayed and is appropriate for sync/rebuild logic.
-  A standout feature of HTML component is its interactive zoom capability: by including a scale property (e.g., "scale": 1) in your data configuration, a slider control will automatically render above the content. This allows end-users to dynamically scale the entire HTML block—including text, images, and layout—from 0.5x to 3.0x. 
- A keyed-persist key function (§13.2) should return plain, JSON-serializable values and read *other* units, not the persisted unit's own value — a key derived from the unit's own state is self-referential and won't behave usefully.
- If a keyed-persist key function raises, the error is logged and that unit's persistence is skipped for the request; it does not fail the request.

## 20. Example Sources in This Repository

- `test_apps/blocks/screens/main.py` (blocks, graph/net, toolbar, interception)
- `test_apps/blocks/screens/zoo.py` (ParamBlock, HTML, pandas table)
- `test_apps/blocks/blocks/tblock.py` (dialogs, table hooks, autocomplete, tree)
- `test_apps/db/screens/single.py` (persistent table basics)
- `test_apps/db/screens/linked.py` (linked persistent tables)
- `test_apps/llm/screens/main.py` (LLM unit/table workflows, `Q` usage)
- `test_apps/persistence/screens/animals.py`, `notes.py` (positional and keyed `persist`)
- `test_apps/proxy/run_blocks.py`, `run_vision.py` (Remote API / `Proxy`)
- `tests/core/test_web_client.py` (`config.web_client` switching, `/default`, and the bundled-client fallback — see §17)