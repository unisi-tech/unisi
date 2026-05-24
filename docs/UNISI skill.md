# UNISI Framework — Developer Reference
## (compiled from source code v2024, /usr/local/lib/python3.12/dist-packages/unisi/)

---

## 1. User Isolation — How It Actually Works

`load_screen()` calls `module_from_spec` + `spec.loader.exec_module(module)`
**separately for every user**.

```python
# users.py:155-159
spec   = importlib.util.spec_from_file_location(name, path)
module = importlib.util.module_from_spec(spec)
module.user = self          # ← user is bound to the module BEFORE exec
spec.loader.exec_module(module)   # ← the entire screen body is re-executed
```

**Consequences:**
- Module-level variables (`_state`, `_run`, any globals) are **isolated per user**.
- `global` inside a handler is safe — it modifies a variable in THAT user's module instance.
- `user` in a screen is the current user object, injected at load time.
- Storing session state in module-level variables is the **correct pattern**.

---

## 2. Screen Lifecycle

### Initial load (once per user connection)
```
load_screen()
  → exec_module()               # executes the .py file body, creates widgets
  → _restore_persist_screen()   # restores persist values from SQLite
  → prepare()                   # called AFTER restore, if defined
```

### Navigating to a screen
```
screen_process(message)
  → screen_module = located screen
  → screen.prepare()            # called on EVERY navigation, if defined
```

### When `prepare()` is needed
`prepare()` is for **cross-widget synchronisation** that cannot be expressed
through initial values: computing derived data, pulling from an external DB,
updating dependent widgets relative to each other.

`prepare()` is **NOT needed** to restore persisted widgets — that is done
automatically by `_restore_persist_screen()` before `prepare()` is called.

---

## 3. The Persist System

### How it works
- `persist=True` on a widget → UNISI saves it to SQLite (`users/<session>.db`)
  on every `value` change.
- Storage: table `state(user_id, namespace, path, value, ts)`.
- Key = widget path in the screen block tree (by `name`).
- On screen load: `_restore_persist_screen` reads the DB and applies the values.

### What NOT to persist
- Large JSON data (LLM results, thousands of rows) — every value change writes
  everything to SQLite. Save such data to a file or external DB manually.
- Data that can be easily recomputed from other persisted widgets — recompute in `prepare()`.

### Tables and persist
A `Table` without `id` is **not persisted**. Its `rows` live in memory only.
On returning to the screen the table will be repopulated only if it has `persist=True`
OR its rows are set inside `prepare()`.

---

## 4. Automatic Change Tracking and Handler Return Values

### How auto-tracking works (sources: units.py, users.py)

`set_reactivity(user)` is called when a screen loads and wraps all widget
attributes in `ChangedProxy`. After that **any assignment**
`widget.value = x` or `widget.rows = [...]` automatically calls
`user.register_changed_unit(widget)`, which adds the widget to
`user.changed_units`.

After the handler finishes, `prepare_result()` collects all accumulated
`changed_units` and sends them to the front end **automatically** — with no
explicit instruction from the handler.

```python
# This is COMPLETELY SUFFICIENT — no return value needed:
def on_click(btn, _):
    my_table.rows = build_rows()   # ← auto-registered
    status.value  = "Done"         # ← auto-registered
    # UNISI will send both widgets to the front end on its own
```

### The only reason to return a Unit from a handler — ROLLBACK

Returning a `Unit` is needed **only** when the front end sent a change (e.g.
the user typed a value into a field) and the server **rejects** it and must
roll the front end back to the previous state. The returned widget carrying
the old value forces the front end to undo its change.

```python
def on_edit(edit, value):
    if not value.strip():
        # Reject the empty value — return the widget with its old value.
        # The front end rolls back to what it had before the input.
        return edit

    edit.value = value   # accept — auto-tracked, will be sent automatically
    # return None is equally correct; auto-tracking already registered the change
```

### Other return types

```python
# Error / Warning / Info — show a message to the user.
# If widgets were also changed, changed_units are merged in.
return Error("Field is required", edit_widget)  # rolls back edit + shows error

# True or Redesign — reload the entire screen
return True

# None — return nothing; all changes are sent automatically
return None   # (or simply omit the return statement)
```

### What NOT to do

```python
# ❌ POINTLESS — UNISI will send all changed widgets anyway
return [table1, status]

# ❌ DOES NOT EXIST in the API
return update(table1, status)

# ✅ CORRECT — just mutate and return nothing
def handler(btn, _):
    table1.rows  = new_rows
    status.value = "OK"
```

---

## 5. Table — `value` and `changed` semantics

The table `value` = the current selection:
- `None` — nothing selected
- `int` — index of the selected row (0-based), when `multimode=False`
- `list[int]` — indices of selected rows, when `multimode=True` or multi-select

```python
# Correct changed-handler signature:
def on_row_selected(table, value):
    # value: int (single) or list[int] (multi)
    if isinstance(value, list):
        for idx in value:
            row = table.rows[idx]
    else:
        row = table.rows[value]

# Binding:
my_table = Table("Title", 0,   # 0 = initial selection
    headers=[...], rows=[])
my_table.changed = on_row_selected
```

The second positional argument `Table(name, value, ...)` is the **initial
selection value**, not a row count or a flag.

---

## 6. Async Handler with Progress

```python
async def long_action(btn, _):
    await user.progress("Starting...")

    # async for — streaming events from an external async generator
    async for event in some_async_generator():
        await user.progress(f"{event['pct']}% - {event['message']}")
        if event['type'] == 'result':
            data = event['data']

    await user.progress(None)   # hide the progress bar

    my_table.rows = build_rows(data)
    status.value  = "Done"
    # no return needed — auto-tracking sends the changes
```

---

## 7. Q and Qx

```python
from unisi import Q, Qx

# Q — structured response (dict with a given type schema)
result = await Q(prompt, {"scenes": list, "characters": list})

# Qx — free-form text response
text = await Qx(prompt, str)

# IMPORTANT: if the prompt contains { } — escape them for Q:
safe_prompt = prompt.replace("{", "{{").replace("}", "}}")
result = await Q(safe_prompt, schema)
```

---

## 8. Toolbar

```python
# Module-level variable — UNISI reads it automatically
toolbar = [Button("Export", on_export, icon="download"),
           my_select_widget]

# Widget in toolbar:
execution_mode = Select("Mode", "Fast", mode_changed,
                        options=["Fast", "Quality"])
toolbar = [execution_mode]
```

---

## 9. Blocks and Layout

```python
# Side by side (horizontal):
blocks = [block_a, block_b]

# Two rows:
blocks = [
    top_block,
    [left_block, right_block],   # second row: two blocks side by side
]

# Nested column:
blocks = [
    wide_left_block,
    [                            # right column, stacked vertically:
        [top_right_a, top_right_b],
        bottom_right_block,
    ]
]
```

---

## 10. Common Mistakes (verified against source code)

| Mistake | Correct |
|---------|---------|
| `return [w1, w2]` to "send" widgets | unnecessary: auto-tracking sends them automatically |
| `return update(w1, w2)` | `update()` does not exist in the API |
| Return a `Unit` to "update" the front end | returning a `Unit` means ROLLBACK — the front end reverts its change |
| `prepare()` to restore persisted widgets | not needed; UNISI does it automatically before `prepare()` |
| Module-level `global x` is unsafe across users | safe: each user gets their own module instance |
| `isinstance(value, list)` in `table.changed` is dead code | wrong: value IS a list when multimode is on |
| `persist=True` on a large TextArea storing JSON | only for small fields; save large data to a file |
| `async def f()` with no `await` inside | remove `async`; callers drop their `await` too |
| `-> AsyncGenerator[dict, None]` on an async generator function | use `-> AsyncIterator[dict]` (PEP 525) |