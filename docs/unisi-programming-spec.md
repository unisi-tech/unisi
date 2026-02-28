# UNISI Framework Programming Documentation and Specification

This document is a programmer-focused specification for building applications with the `unisi` framework.  
It is based on:
- `/home/george/Downloads/UNISI Tech (2).pdf`
- the repository code (`unisi/*.py`)
- working examples in `tests/blocks`, `tests/db`, and `tests/llm`

## 1. Purpose and Scope

UNISI provides:
- automatic web GUI rendering from Python objects
- synchronized client/server state
- event-driven handlers (sync and async)
- optional services: hot reload, autotest, DB-backed tables, LLM-assisted fields, API handlers

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
| `db_dir` | str/None | `None` | DB path for persistent tables |
| `lang` | str | `"en-US"` | UI language |
| `public_dirs` | list[str] | `[]` | Extra static roots |
| `image` | str | `"icons/favicon-32x32.png"` | App icon |

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
ParamBlock(name, *units, row=3, **params)
```

Parameter mapping:
- `bool` -> `Switch`
- `str/int/float` -> `Edit`
- `(value, [options])` -> `Select` or `Range`
- `(value, dict_tree)` -> `Tree`

Read current values:

```python
params = param_block.params
```

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

- `Button(name, handler=None, **kwargs)`
- `Edit(name, value?, changed?, **kwargs)`
- `Text(name, ...)` (read-only label style)
- `TextArea(name, value?, changed?, **kwargs)`
- `Range(name, value?, changed?, options=[min,max,step])`
- `Switch(name, value=False, changed?)`
- `Select(name, value?, options=[])`
- `Tree(name, value?, options=dict|list)`
- `Chart(name, option, changed?)`
- `HTML(name, html_string, changed?)`
- `Image(name_or_url, value=False, handler=None, label="", width=300, ...)`
- `Video(name, ...)`
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

Persistent DB mode (requires `config.db_dir`):
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

## 13. LLM Integration Specification

Two levels:
1. Unit/Table `llm` dependency auto-fill
2. Explicit async queries via `Q` and `Qx`

### 13.1 Unit and Table `llm`

Examples:

```python
ebirth = Edit("Date of birth", llm=True)              # infer from block context
occupation = Edit("Occupation", llm=ename)            # infer from one dependency
table = Table("Persons", llm={"Date of birth": "Name", "Occupation": True}, ...)
```

### 13.2 Explicit queries

`Q(prompt, type_value=..., **format_vars)` returns an awaitable with typed JSON validation.

```python
country_info = await Q(
    "Provide information about {country}.",
    dict(capital=str, population=int, currency=str),
)
```

`Qx` is raw/non-extended prompt mode.

LLM provider is configured through `config.llm`.

## 14. HTTP Route Integration

You can add custom aiohttp routes while keeping UNISI runtime:

```python
from aiohttp import web
import unisi

async def handle_get(request):
    return web.Response(text=request.query_string)

unisi.start(http_handlers=[web.get("/get", handle_get)])
```

## 15. Shared Blocks and Reuse Pattern

Place reusable block modules in `blocks/` and import into screens:

```python
from blocks.tblock import config_area
blocks = [config_area]
```

Use interception (`@handle`) in screen module when you need screen-specific behavior overrides for shared units.

## 16. End-to-End Example (Runnable Pattern)

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

## 17. Behavior Notes and Constraints

- Screen and block names should be unique in their active context.
- For DB-backed `Table`, `config.db_dir` must be set; otherwise creation fails.
- If a handler is missing, `changed` events assign incoming value directly.
- Dialog remains active if callback returns message/update that keeps it open.
- `prepare()` runs when screen is displayed and is appropriate for sync/rebuild logic.

## 18. Recommended Development Workflow

1. Create `config.py` and `screens/main.py`.
2. Start with plain `Edit`, `Button`, `Table`.
3. Add `changed` handlers and explicit return values.
4. Extract reusable blocks into `blocks/`.
5. Add `@handle` interception only for overrides.
6. Enable DB mode (`db_dir`) and LLM mode (`llm`) when needed.

## 19. Example Sources in This Repository

- `tests/blocks/screens/main.py` (blocks, graph/net, toolbar, interception)
- `tests/blocks/screens/zoo.py` (ParamBlock, HTML, pandas table)
- `tests/blocks/blocks/tblock.py` (dialogs, table hooks, autocomplete, tree)
- `tests/db/screens/single.py` (persistent table basics)
- `tests/db/screens/linked.py` (linked persistent tables)
- `tests/llm/screens/main.py` (LLM unit/table workflows, `Q` usage)

