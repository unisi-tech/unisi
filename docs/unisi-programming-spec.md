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
| `autotest` | bool/str | `False` | Autotest mode or pattern |
| `logfile` | str/None | `None` | Optional log file |
| `mirror` | bool | `False` | Mirror screens across sessions |
| `share` | bool | `False` | Shared sessions mode |
| `profile` | int | `0` | Profiling mode |
| `llm` | tuple/list/None | `None` | LLM provider config |
| `llm_cache` | str (optional) | unset | Cache file for LLM calls |
| `db_path` | str/None | `None` | DB file path for persistent tables (or set `UNISI_DB_PATH` env var) |
| `lang` | str | `"en-US"` | UI language |
| `public_dirs` | list[str] | `[]` | Extra static roots |
| `image` | str | `"icons/favicon-32x32.png"` | App icon |
| `session` | str/None | None | optional session/user id for debugging |

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

Linked tables:

```python
otable = Table(
    "Orders",
    id="Orders",
    rows=orders,
    headers=["name", "sum"],
    link=(utable, {"type": "string", "weight": "double"}),
)
```

> Table's persistent DB mode manages application data rows and is a separate system from Unit/Screen state persistence (`persist=...`, §13).

## 13. State Persistence Specification

Any `Screen`, `Block`, or `Unit` can opt into having its state survive across requests — and, for screens, be restored when the screen is next loaded — by setting `persist`. There are two distinct modes depending on what you pass.

### 13.1 Positional persist (`persist=True`)

The default mode. Set `persist=True` on:
- a screen module (module-level `persist = True`, or `screen.persist` in `prepare()`) — persists every unit on that screen;
- an individual `Block` or `Unit` — persists just that subtree.

Storage is **keyed by the unit's position** in the screen tree (its name chain from the screen down to the unit), scoped to the current user session. Whenever a persisted unit changes, its current state is saved; when the screen is next loaded, the saved state is restored onto the unit at the same tree position. This is the default, screen-relative case; a block imported from `blocks/` and reused across screens anchors position and identity differently — see §13.6.

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
# unit on screen "orders", tree path "form/price", persist=lambda: (selected_row.value,)
user.get_objects("orders", "form/price", "..")
# -> {'["Widget"]': {...saved fields...}, '["Gadget"]': {...saved fields...}}
```

`context_template` behaves like `get_keys`'s template when it contains `..` (prefix/suffix match). Unlike `get_keys`, a template with no `..` is not an error — it's an exact `context_key` match instead, so `get_objects` also covers a single positional-persist lookup (`context_key=""`, §13.1):

```python
user.get_objects("orders", "form/price", '["Widget"]')   # exact match -> that one record, or {}
user.get_objects("settings_screen", "panel/state", "")   # exact "" -> the persist=True save, if any
```

Returns `{context_key: fields_dict}`, empty if nothing matches or the session has no DB yet. Read-only.

`get_contexts(namespace, path, context_template)` — same parameters, same exact-vs-template rule, but returns just the matching context_keys as a `list[str]` instead of a `{context_key: fields}` dict, without reading or decoding the stored fields at all. Cheaper than `get_objects` when you only need to know which records exist:

```python
user.get_contexts("orders", "form/price", "..")   # -> ['["Widget"]', '["Gadget"]']
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

`theme` persists under `('@blocks.header', 'Theme')` regardless of which screen imports `header_block`, including a screen that has never declared its own `persist = True` and is the very first one loaded this session. (A field that is *not* separately named at module level — created inline as one of `header_block`'s children instead — gets a path measured from `header_block` down to it instead, e.g. `'Header@Theme'`; either way the anchor is the block's module, never the hosting screen.)

To look a specific unit's saved record up directly (e.g. via `get_objects`/`get_contexts`, §13.4) without hardcoding which namespacing scheme applies, use `user.persist_location(unit)`, which returns the `(namespace, path)` currently in effect for it.

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

`Q(prompt, type_value=..., **format_vars)` returns an awaitable with typed JSON validation.

```python
country_info = await Q(
    "Provide information about {country}.",
    dict(capital=str, population=int, currency=str),
)
```

`Qx` is raw/non-extended prompt mode.

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

## 17. End-to-End Example (Runnable Pattern)

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

## 18. Behavior Notes and Constraints

- Screen and block names should be unique in their active context.
- For DB-backed `Table`, `config.db_path` (or `UNISI_DB_PATH`) must be set; otherwise creation fails.
- If a handler is missing, `changed` events assign incoming value directly.
- Dialog remains active if callback returns message/update that keeps it open.
- `prepare()` runs when screen is displayed and is appropriate for sync/rebuild logic.
-  A standout feature of HTML component is its interactive zoom capability: by including a scale property (e.g., "scale": 1) in your data configuration, a slider control will automatically render above the content. This allows end-users to dynamically scale the entire HTML block—including text, images, and layout—from 0.5x to 3.0x. 
- A keyed-persist key function (§13.2) should return plain, JSON-serializable values and read *other* units, not the persisted unit's own value — a key derived from the unit's own state is self-referential and won't behave usefully.
- If a keyed-persist key function raises, the error is logged and that unit's persistence is skipped for the request; it does not fail the request.

## 19. Example Sources in This Repository

- `tests/blocks/screens/main.py` (blocks, graph/net, toolbar, interception)
- `tests/blocks/screens/zoo.py` (ParamBlock, HTML, pandas table)
- `tests/blocks/blocks/tblock.py` (dialogs, table hooks, autocomplete, tree)
- `tests/db/screens/single.py` (persistent table basics)
- `tests/db/screens/linked.py` (linked persistent tables)
- `tests/llm/screens/main.py` (LLM unit/table workflows, `Q` usage)