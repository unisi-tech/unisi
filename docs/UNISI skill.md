# UNISI Framework — Agent Reference
## Verified against `unisi` 0.7.7 source (github.com/unisi-tech/unisi)

This file is a companion to the other docs in this folder, not a replacement for
them. It exists for one purpose: to keep an AI coding agent from writing plausible
but wrong UNISI code by stating precisely how the runtime behaves, citing the
source file that proves it, and flagging every place a natural assumption —
from Python habits, or from a plausible-sounding guess — turns out to be false.

**Read this file when you're about to write or modify UNISI application code.**
Read the others when you need exhaustive detail on one subsystem:

| File | What it's for |
|---|---|
| `README.md` | Marketing-level tour + widget-by-widget syntax reference |
| `docs/unisi-quickstart.md` | 5-minute path to a running app |
| `docs/unisi-programming-spec.md` | The full formal spec — every constructor, every option, numbered §-sections |
| `docs/persistent_tables.md` | DB-backed `Table(id=...)`, links, schema evolution, geo-spatial fields, full `Dbtable` API |
| `docs/voicecom.md` | Voice-command subsystem (modes, vocabulary, extending it) |
| `docs/protocol.md` | The WebSocket wire protocol — verified against a live server, needed for `config.web_client` |
| **this file** | Verified internals, gotchas, and everything the other docs under-cover |

If your installed version differs from 0.7.7, anything version-sensitive below is
worth re-checking against the actual source under `site-packages/unisi/`.

---

## 1. Per-User Isolation — How It Actually Works

### Screens
`ModulesMixin.compile_screen()` (`unisi/modules.py`) calls `module_from_spec` +
`spec.loader.exec_module(module)` **separately for every user, for every screen
they visit**:

```python
# unisi/modules.py — ModulesMixin.compile_screen()
spec   = importlib.util.spec_from_file_location(name, path)
module = importlib.util.module_from_spec(spec)
module.user = self          # user is bound to the module BEFORE exec
spec.loader.exec_module(module)   # the entire screen body re-runs
```

Consequences:
- Module-level variables in a `screens/*.py` file are isolated per user.
- `global` inside a handler is safe — it mutates a variable in *that user's own*
  module instance.
- `user` in a screen/block module is injected as a plain module attribute, not
  imported — UNISI also writes `typings/__builtins__.pyi` declaring `user: User`
  on every `unisi.start()`, so Pylance/Pyright stop flagging it as undefined.
- Storing session state in module-level variables is the *correct*, intended
  pattern, not a workaround.

### Shared blocks — same isolation, different mechanism
A `blocks/*.py` module (imported with a normal `from blocks.x import y`) is
**not** a process-wide Python import singleton the way it would be in an
ordinary script. `ModulesMixin._install_modules()` / `_capture_modules()`
(`unisi/modules.py`) strip a user's own captured `blocks.*` entries out of the
real `sys.modules` before that user's screen executes, and put back only
*that same user's* previously-captured versions — then re-capture and remove
them again once the screen finishes compiling. Net effect:
- A block is imported (its top-level code runs) **once per user**, the first
  time that user visits any screen that imports it.
- It stays the *same live object* for every other screen **that same user**
  later visits — this is what "shared block" means.
- A different user gets a completely separate execution and separate objects.
  There is no cross-user leakage through a `blocks/` module, even though plain
  Python import caching would normally produce exactly that.

### Prior-session summaries
If you're resuming a task from a summary that says "user X's shared block
already handles session state safely" — verify it against the mechanism above
rather than trusting the summary; it is easy to describe this backwards.

---

## 2. Screen Lifecycle

### Initial connect (once per user)
```
User.__init__() → load_lazy()
  → load_screen() → compile_screen()   # exec_module(), builds the Screen object
  → _finish_loaded_screen(prepare=True)
      → _mark_persist_units() → _restore_persist_screen()   # restore BEFORE prepare
      → module.prepare()                                     # only if defined
```

### Navigating to a screen (`screen_process`, every time, including revisits)
```
screen_process(message)
  → ensure_screen(name)        # returns cached module if already visited,
                                #   otherwise loads+restores it fresh (prepare NOT
                                #   called yet at this point either way)
  → s.screen.prepare()         # called here, unconditionally, if defined —
                                #   on the FIRST visit AND on every later revisit
```

`prepare()` is for cross-widget synchronization that can't be expressed as
static initial values: pulling from a DB, computing derived fields, wiring
dependent widgets to each other. It runs *after* persisted state has already
been restored, so it never needs to restore persistence itself — that part is
automatic and happens earlier in the pipeline no matter which of the two paths
above triggered the load.

---

## 3. Runtime Configuration (`config.py`) — Full Reference

Defaults live in `unisi/utils.py` (`set_defaults(config, {...})`) plus a few
more read directly by `unisi/multimon.py` and `unisi/llmrag.py`. This table is
the union of all three — the programming spec's own config table is missing
several of these.

| Key | Default | Meaning |
|---|---|---|
| `port` | `8000` | HTTP/WebSocket port |
| `appname` | `"Unisi app"` | Default header / page title |
| `upload_dir` | `"web"` | Where uploads land; also served statically |
| `hot_reload` | `False` | Watch `screens/`/`blocks/` and live-reload on save |
| `autotest` | `False` | `False` off · `True`/`'*'` run every fixture in `autotest/` · `[...]` a specific filename list — see §16 |
| `logfile` | `None` | Log file path; `None` = console only |
| `mirror` | `False` | New anonymous connections start as a reflection of the most recent user — see §12 |
| `share` | `False` | Enables `?session=` reattachment — see §12 |
| `profile` | `0` | Log a warning if a handler takes longer than this many seconds |
| `froze_time` | `None` | Log a warning if a session sits waiting longer than this many seconds. **Not `freeze_time`** — see the note below |
| `monitor_tick` | `0.005` | Poll interval (seconds) for the monitor process and for `run_process`'s progress relay |
| `pool` | `None` | Passed straight to `multiprocessing.Pool(pool)` — the worker pool `user.run_process()` uses (§7); `None` = `os.cpu_count()` |
| `db_path` | `None` | SQLite file for persistent `Table(id=...)`; or set `UNISI_DB_PATH` env var |
| `lang` | `"en-US"` | UI language |
| `public_dirs` | `[]` | Extra static-file roots |
| `web_client` | `None` | Root dir of a custom UNISI-protocol web client to serve at `/` instead of the bundled one, which stays reachable at `/default` — see §15 |
| `debug` | `False` | (read by the runtime; no further behavior verified here) |
| `session` | `None` | Force a fixed session id — mainly for debugging/scripted connections |
| `image` | `"icons/favicon-32x32.png"` | App icon |
| `llm` | `None` | LLM provider config — see §8 |
| `llm_cache` | unset | Directory path — enables a persistent disk cache for `Q()`/`Qx()` |
| `llm_cache_ttl` | `None` | Cache entry lifetime in seconds; `None` = never expires |
| `temperature` | `0.0` | LLM sampling temperature |
| `strict_schema` | `True` | Whether structured `Q()` calls request strict JSON-Schema enforcement (auto-falls back per-model if a provider rejects it) |
| `reasoning` | unset | Effort level (e.g. `'medium'`) forwarded as `extra_body.reasoning` for reasoning models |
| `persist` | `False` | Global "persist every unit on every screen" switch — same effect as `persist = True` on every individual screen module |

> **`froze_time`, not `freeze_time`.** That's the real spelling
> (`unisi/multimon.py` does `from config import froze_time, ...`) — a very
> guessable typo, since "freeze" is the standard English spelling. Writing
> `freeze_time` in your `config.py` silently does nothing — no error,
> monitoring just never activates.

`config.py` is auto-created with sane defaults on first run if missing. If it
exists but was placed outside the working directory, `unisi` prints an error
and exits rather than guessing.

---

## 4. Automatic Change Tracking and Handler Return Values

### How auto-tracking works (`unisi/units.py`, `unisi/users.py`)

`set_reactivity(user)` runs when a screen (or a shared block reused by it)
loads, and wraps every widget attribute in `ChangedProxy`. After that, **any
assignment** — `widget.value = x`, `widget.rows.append(row)`, `widget.rows[i]
= x` — automatically calls `user.register_changed_unit(widget)`.

```python
# COMPLETELY SUFFICIENT — no return value needed:
def on_click(btn, _):
    my_table.rows = build_rows()   # auto-registered
    status.value  = "Done"         # auto-registered
    # UNISI sends both widgets to the client on its own
```

### The only reason to return a `Unit` from a handler — ROLLBACK

Returning a `Unit` is needed **only** when the client already sent a change
(the user typed into a field) and the server rejects it and must roll the
client back to the previous value. The returned widget — still carrying its
old value, since you never assigned the new one — forces the client to undo
its own edit.

```python
def on_edit(edit, value):
    if not value.strip():
        return edit          # reject: client rolls back to what it had before

    edit.value = value        # accept — auto-tracked, sent automatically
    # return None is equally correct here
```

### Other return types

```python
return Error("Field is required", edit_widget)   # rolls back + shows a message;
                                                    # any already-changed widgets
                                                    # are still merged in
return True        # or Redesign — force a full screen reload
return None         # nothing to add; changes are sent automatically anyway
```

### What NOT to do

```python
return [table1, status]      # ❌ pointless — sent automatically regardless
return update(table1, status)  # ❌ update() does not exist in the API

def handler(btn, _):           # ✅ just mutate and return nothing
    table1.rows  = new_rows
    status.value = "OK"
```

---

## 5. Event Interception — `handle()`

```python
from unisi import handle, Warning

@handle(selector, "changed")
def reject_based(unit, value):
    if value == "Based":
        return Warning("Mode cannot be Based", unit)
    return unit.accept(value)
```

- `handle(unit, event)` registers against whichever `User` is "current" at
  *registration time* — normal for code inside a screen module, since it's
  compiled synchronously for one specific user. A persistent `Table()`
  declared at plain module level (constructed once, before any `User` exists)
  is handled too: the registration is held in `Unishare.pending_handlers` and
  copied into every subsequently-constructed `User`.
- **It composes, it does not replace.** If the target already has a `changed`
  handler (or another `@handle` was already registered for the same
  `unit, event` pair), both run in registration order via `compose_handlers`
  — this is how UNISI itself layers extra behavior onto a `Table`'s own
  `changed`/`search`/`filter` handlers for linked tables (`unisi/tables.py`).
  Their non-`None` results are merged (deduplicated); **if any handler in the
  chain returns `True` or `Redesign`, the chain stops immediately** and later
  handlers in it do not run.
- Use it for screen-specific overrides of a shared block's default behavior —
  see `unisi-programming-spec.md` §9 and §16 for the full contract.

---

## 6. Table — `value` and `changed` Semantics

`value` is the current selection, not a row count or a flag:
- `None` — nothing selected
- `int` — index of the selected row (0-based), when `multimode=False`
- `list[int]` — indices of selected rows, when multi-select is active

```python
def on_row_selected(table, value):
    if isinstance(value, list):     # multi-select IS a real, live code path —
        rows = [table.rows[i] for i in value]   # do not assume it's dead code
    else:
        row = table.rows[value]

my_table = Table("Title", 0, on_row_selected, headers=[...], rows=[])
```

`Table(name, value, ...)` — the second positional argument is the **initial
selection**, not a row count.

For DB-backed persistent tables (`Table(id=..., link=...)`, many-to-one /
many-to-many, schema evolution, geo-spatial `point` fields, the full
`Dbtable` read/write API) — that is a large, separate system from Unit/Screen
persistence (§9 below). See `persistent_tables.md` in full; don't try to
reconstruct it from `unisi/db.py` (1800+ lines) on the fly.

The two `link=` shapes are easy to conflate — the *shape* of `self.link`
decides many-to-one vs many-to-many, not whether the payload dict is empty:
- `link=parent_table` (bare, no list/tuple) → many-to-one, FK column
  `link_id` added to this table, no junction table.
- `link=[parent_table, {...}]` (list or tuple, 2 or 3 elements) →
  many-to-many, junction table — **even when the dict is `{}`**. The
  plausible-but-wrong assumption: "`{}` means no extra fields, so this must
  be the many-to-one case." It isn't — the list/tuple wrapper is what
  selects many-to-many, independent of what (if anything) the dict holds.
  `unisi/tables.py`, `Table.__init__`.

---

## 7. Async Handlers, Progress, and Offloading Heavy Work

```python
async def long_action(btn, _):
    await user.progress("Starting...")

    async for event in some_async_generator():
        await user.progress(f"{event['pct']}% - {event['message']}")

    await user.progress(None)    # hide the progress bar

    my_table.rows = build_rows()
    status.value  = "Done"
    # no return needed — auto-tracking sends the changes
```

`await user.progress(None)` on completion is optional but tidy — UNISI hides
it on its own once the handler returns, this just does it earlier.

### CPU-bound / blocking work — do not call it inline

A synchronous, CPU-heavy loop (or any blocking call) run directly inside a
handler blocks the single asyncio event loop for **every connected user**,
not just the one who triggered it. Offload it:

```python
def crunch_numbers(n):          # a plain, picklable, top-level function —
    return sum(i * i for i in range(n))   # runs in a worker process, not this one

async def on_click(btn, _):
    result = await user.run_process(crunch_numbers, 10_000_000)
    status.value = f"Result: {result}"
```

`user.run_process()` (`unisi/users.py`) delegates to
`run_external_process()` (`unisi/multimon.py`), which submits the call to a
`multiprocessing.Pool` sized by `config.pool` and polls for the result every
`config.monitor_tick` seconds without blocking the event loop.

For progress updates from inside the worker, the contract is specific: pass
`None` (or an existing `Queue`) as the task's **last positional argument**,
and the worker must end by putting a `None` sentinel on it —

```python
def crunch_with_progress(n, queue):        # queue is injected as the last arg
    for i in range(n):
        if i % 100_000 == 0:
            queue.put(f"{i}/{n}")
    queue.put(None)                         # required — signals completion
    return n * n

async def on_click(btn, _):
    result = await user.run_process(
        crunch_with_progress, 1_000_000, None,   # None here = auto-create the queue
        progress_callback=user.progress,          # async def progress(self, value, *updates)
    )
    status.value = f"Result: {result}"
```

Calling `run_process(..., progress_callback=cb)` with *no* positional args at
all raises `ValueError` — the queue has nowhere to go.

---

## 8. LLM Integration — `Q()` / `Qx()`

```python
from unisi import Q, Qx

result = await Q("Details about {name}", dict(age=int, city=str), name=name)
text   = await Qx("Free-form prompt, sent exactly as written")
```

### Two things people reflexively do that are both wrong here

1. **Do not escape `{`/`}`.** `_safe_format` (`unisi/llmrag.py`) only
   substitutes `{key}` tokens whose `key` is one of the kwargs you actually
   passed; every other brace — literal JSON in the prompt, code samples,
   anything — is left completely untouched. `prompt.replace('{', '{{')`
   before calling `Q()` is not just unnecessary, it lands literal double
   braces in what the model sees.
2. **Pass every placeholder explicitly.** `Q()` does not read matching
   variable names out of the caller's local scope. An unfilled `{name}` in
   the prompt is left as literal text, not an error — so a forgotten kwarg
   fails silently (the model just sees `{name}` verbatim) rather than
   raising. Double-check every `{...}` in a prompt has a matching kwarg.

### Signature

```python
Q(str_prompt, type_value=str, blank=True, extend=True, format=True,
  images=None, **format_vars) -> Any
Qx(str_prompt, type_value=str, images=None) -> Any     # == Q(..., extend=False, format=False)
```

- `type_value` — expected type (`str`, `int`, `list[str]`, `dict(...)`,
  `dict[str, str]`, `'date'`, ...); drives both the format instruction sent to
  the model and validation of the parsed result.
- `blank` — reserved for compatibility, currently unused; don't rely on it.
- `extend` — prepend a system/identity prefix + "don't add commentary"
  instruction. `Qx` sets this `False` for a bare prompt.
- `format_vars['identity']` overrides the default persona string
  (`"You are an intelligent and extremely smart assistant."`) and is popped
  out before substitution, so `identity` itself can't double as a `{identity}`
  placeholder target.
- `images` (both `Q` and `Qx`) — a single value or list, for vision models.
  Each item is a `'http(s)://...'` URL, a `'data:image/...;base64,...'` URI,
  any other `str` (read as a local file path and base64-encoded), `bytes` /
  `bytearray` (base64-encoded, MIME sniffed), or a dict for manual control
  (`{'url':...}`, `{'path':...}`, `{'data': b'...', 'mime': '...'}`, optional
  `'detail'`). `None` (default) sends nothing and leaves the cache key
  identical to a pre-`images` call. Whether the provider actually accepts
  image input isn't checked in advance — its own error surfaces normally.

### Caching

Set `config.llm_cache = '<directory>'` (+ optional `config.llm_cache_ttl`) to
persist successful `Q()`/`Qx()` results across restarts via `diskcache`. A
malformed/invalid response is never written to the cache, so a bad answer
can't get "stuck" there forever.

### Provider configuration (`config.llm`)

```python
llm = ['host', 'http://localhost:1234/v1']                          # local, no key
llm = ['host', address, 'MY_KEY_ENV', 'model-name']                 # local/custom, with a key
llm = ['groq', 'llama3-8b-8192']                                     # cloud provider
llm = ['openai', 'gpt-5.1', 'https://my-proxy.example.com/v1']       # cloud provider, custom base URL
```

Must be a list or tuple of 2–4 items matching one of the shapes above —
`config.llm = 'openai'` alone, or any other shape, logs
`Invalid config.llm format` to the console and leaves LLM features off
(`Q()`/`Qx()` then simply have nothing to call).

| Provider string | Needs |
|---|---|
| `'host'` | An `address` (any OpenAI-compatible endpoint: LM Studio, Ollama, LlamaCpp, OpenRouter, ...); API key optional |
| `'openai'` | `OPENAI_API_KEY` |
| `'groq'` | `GROQ_API_KEY` |
| `'google'` / `'gemini'` | `GOOGLE_API_KEY` |
| `'mistral'` | `MISTRAL_API_KEY` |
| `'xai'` | `XAI_API_KEY` |

All of these — including the named cloud providers — go through the same
`AsyncOpenAI` client with a per-provider `base_url`; there's no separate
SDK per provider. `config.strict_schema = False` if a given provider rejects
strict JSON-Schema mode; UNISI also auto-detects and remembers a
model-specific rejection of `strict` or a non-default `temperature` after the
first failure, so most of the time you don't need to touch this.

### Per-Unit / per-Table auto-fill

```python
ebirth = Edit("Date of birth", llm=True)              # infer from block siblings
occupation = Edit("Occupation", llm=ename)             # infer from one dependency
table = Table("Persons", llm={"Date of birth": "Name", "Occupation": True}, ...)
```

Recomputed automatically whenever a dependency's value changes and this
field's own value is empty — see `unisi-programming-spec.md` §14.1, and
`test_apps/llm/screens/main.py` for a complete working example (also the
canonical example for explicit `Q()`/`Qx()` calls, `asyncio.gather` fan-out,
and image/geo-style queries).

---

## 9. State Persistence — Four Mechanisms

Full reference: `unisi-programming-spec.md` §13 (13.1–13.7). This section is
a decision guide plus the facts most likely to be assumed wrong.

| Mechanism | You write | Saves | Use it for |
|---|---|---|---|
| Positional (`persist=True`) | `Unit(..., persist=True)` on a Unit / Block / screen module | that widget (or block subtree) at its fixed tree position | "remember what this widget was last set to for this user" |
| Keyed (`persist=<fn>`) | `Unit(..., persist=lambda: (key,))` | a separate value **per key**, recomputed every request | a widget reused to show different records — "remember a different value per selected row" |
| Simple key-value | `user.set_key(k, v)` / `user.get_key(k)` / `get_keys("prefix..")` | arbitrary strings, not tied to any widget or screen | app-level session settings |
| On-demand snapshot | `user.persist_units(*units)` / `user.restore_units(*units)` | whatever you name, only when you call it | Save/Revert buttons, checkpoints |

```python
# Keyed persist — a REAL example, test_apps/persistence/screens/animals.py:
table = Table("animals", headers=["name", "age", "positive"], rows=[...], persist=True)

def row_key():
    index = table.value
    if index is not None:
        return table.rows[index][0]        # e.g. "Dog", "Cat" — the row name

link = Edit('wiki link', '', persist=row_key)   # a DIFFERENT link remembered per animal
```

Notes:
- **The storage schema has no `user_id` column.** Each session already gets
  its own SQLite file at `users/<session-id>.db` (WAL mode); the table itself
  is `state(namespace, path, context_key, value, ts)` — the isolation is the
  separate file, not a row-level key.
- **Positional persist does not rewrite every persisted widget on every
  change.** Only the unit(s) that actually changed this request get written
  — one row per changed unit (or per changed Block, if the Block itself is
  what carries `persist=True`). The real cost to watch for is a *single*
  field whose own value is large (e.g. a `TextArea` holding megabytes of LLM
  output) and changes often — that one row gets rewritten every time. For
  that: write to a file/DB yourself, or use `user.persist_units()` to
  snapshot it only at explicit moments instead of on every change.
- Persistence is automatically disabled during autotest runs
  (`user.testing`) — don't design a recorded fixture that depends on it.
- A widget inside a `blocks/` module keeps its persisted state under a
  namespace anchored to the block's own module (`'@blocks.header'`, not a
  screen name) — see §13.6 for why, and `user.persist_location(unit)` if you
  need to look a specific unit's row up directly.

---

## 10. Widget Quick Reference

Every constructor below also accepts `persist=` (§9) and, except `Button`, `llm=` (§8).

| Constructor | Notes |
|---|---|
| `Button(name, handler=None, **kwargs)` | `type='command'`; name starting with `_` hides the label (icon-only) |
| `CameraButton(name, handler=None, **kwargs)` | `Button` with `type='camera'` |
| `UploadButton(name, handler=None, **kwargs)` | `Button` with `type='uploader'`; default `width=250` |
| `Edit(name, value='', changed=None, **kwargs)` | type auto-detected: `'number'` if `value` is `int`/`float`, else `'string'` |
| `Text(name, ...)` | read-only label; `value` forced to `name`, `edit=False` |
| `TextArea(name, value?, changed?, **kwargs)` | multi-line |
| `Range(name, value=1.0, changed?, options=[min,max,step])` | default options `[value-10, value+10, 1]` if omitted |
| `ContentScaler(name='Scale content', value=1.0, **kwargs)` | a `Range` wired to rescale a set of elements' `width`/`height`; default zoom range is `0.25`–`3.0` in steps of `0.25`; auto-created when you pass `Block(..., scaler=True)` — you rarely construct it directly |
| `Switch(name, value=False, changed?, type?)` | `type`: `'switch'` (default) or `'check'` |
| `Select(name, value?, changed?, options=[])` | `type` auto: `'radio'` if ≤3 options, else `'select'`; explicit `type='list'` renders a vertical list instead |
| `Tree(name, value?, changed?, options={child: parent})` | root items map to `parent=None` |
| `Image(path_or_url, value=False, handler?, label='', width=300, url?)` | click toggles a selection checkmark |
| `Video(name, value?, changed?, fragments=[])` | `value = {"position": float, "play": bool, "sound": bool}` |
| `Sound(name, value?, handler?)` | `value = {"url", "play", "position", "volume"}` |
| `Chart(name, option, changed?)` | raw ECharts `option` dict passed as the constructor's value, stored on `.option` (`.value` becomes the current selection) |
| `HTML(name, html_string, changed?, scale?, **kwargs)` | a raw HTML/JS content container (`type='html'`); the Python side does no special handling of any kwarg beyond the base `Unit` constructor — `scale` (e.g. `1`) is a frontend-only convention that renders a slider zooming the content from 0.5× to 3.0× |
| `Graph(name, value?, changed?, nodes=[Node(...)], edges=[Edge(...)])` | `value = {'nodes': [...], 'edges': [...]}` selection |
| `Net(name, value?, topology=None, **kwargs)` | a `Graph` auto-built from a topology of *Units* (screen/block/unit map) instead of manual nodes/edges |
| `Table(name, value?, changed?, **kwargs)` | see §6; DB-backed mode → `persistent_tables.md` |
| `Table(name, panda=df, **kwargs)` | pandas-backed (`PandaTable`) — same append/delete/modify hooks, operating on the DataFrame |
| `Block(name, *children, **options)` | `closable=True` gives it a `.close` that removes it from `user.screen.blocks`; `scaler=True` auto-adds a `ContentScaler` (above); any other option becomes a plain attribute for the frontend to read |
| `ParamBlock(name, *units, changed=None, row=3, strict='recurse', persist=False, **params)` | value-type → widget mapping and reassignable `.params` — full contract in the spec §7 |
| `Dialog(question, callback, *content, commands=['Ok','Cancel'], icon=?)` | first `commands` entry renders as primary |

---

## 11. Blocks, Toolbar, and Layout

```python
blocks = [block_a, block_b]                 # side by side
blocks = [top_block, [left, right]]         # a row, then a row of two
blocks = [wide, [[a, b], bottom]]           # nested column on the right
```

A plain sequence lays out one way; a *nested* sequence inside it defines a
sub-area — see README's "Block details" section for the full visual-layout
rule.

```python
toolbar = [Button("Export", on_export, icon="download"), execution_mode]
```

- `toolbar` is a module-level list, read automatically per screen.
- `User.toolbar` (a *class*-level list) is appended onto every screen's
  toolbar automatically at compile time (`compile_screen`, `unisi/modules.py`)
  — this is how the "Add test" recorder button (§15) shows up on every
  screen once `config.autotest` is enabled, without any screen author doing
  anything.

---

## 12. Multi-User Modes

Both are `False` by default — a fresh, fully independent session per
connection.

- **`share = True`** — a client that reconnects with the same `?session=`
  query param (or `Proxy(session=...)`, §13) joins the *same* logical session
  as an additional live "reflection." All reflections stay in sync in real
  time via `user.broadcast()`; each keeps whatever screen it happens to be on.
  Use this for a second tab/device mirroring one logical session, or for the
  Remote API driving the same session a human is watching.
- **`mirror = True`** — every new anonymous connection starts as a reflection
  of the most recently connected user (`User.last_user`), but always on that
  user's *first* screen (`screens[0]`), not wherever that user currently is —
  good for a kiosk/public display that should always come up on the home
  screen. Also disables the voice module by default per screen
  (`Screen.defaults['voice'] = not config.mirror`).

---

## 13. Remote API (`Proxy`)

```python
from unisi import Proxy, Event

proxy = Proxy('localhost:8000', session='<id>', screen='Main')  # session/screen optional
if proxy.set_screen("Image analysis"):
    if proxy.command_upload('Load an image', image_file) & Event.update:
        table = proxy.element('Image classification')
proxy.close()
```

- `session=` reattaches to an existing session (only meaningful with
  `config.share = True` on the server — §12); `screen=` activates a screen
  immediately on connect.
- `Event` is an `IntFlag` — combine/test with `&`/`|` as in the example above:
  `update=1`, `invalid=2`, `message=4`, `progress=8`, `unknown=16`,
  `dialog=32`, `screen=65`, `complete=128`, `append=256`, plus the combined
  `update_message=5`, `update_progress=9`, `unknown_update=17`,
  `update_complete=129`, `update_append=257`.
- Full method table (`command`, `command_upload`, `element`, `elements`,
  `interact`, `screen_menu`, `set_screen`, `set_value`, `close`) is in the
  README.

---

## 14. Custom HTTP Routes

```python
from aiohttp import web
import unisi

async def handle_get(request):
    return web.Response(text=request.query_string)

unisi.start(http_handlers=[web.get("/get", handle_get)])
```

`unisi.start(user_type=User, http_handlers=None)` adds your routes *before*
UNISI's own (`/ws`, upload static route, catch-all static, upload POST) —
your paths win on a collision.

---

## 15. Custom Web Client (`config.web_client`)

```python
# config.py
web_client = 'custom_client/dist'   # relative (to cwd) or absolute
```

Points `GET /` at a separate, already-built front end instead of the
bundled Quasar client — as long as that front end speaks the UNISI protocol
(opens a WebSocket to `/ws`, exchanges the same JSON messages — full wire
format in `docs/protocol.md`), swapping
`web_client` never touches the protocol itself, only which static files
answer `/`.

Resolution order inside `static_serve()` (`unisi/server.py`), all through
the same traversal-safe `resolve_in_root()`:

1. `/default` or `/default/<anything>` — **always** the bundled client
   (`webpath`), regardless of `config.web_client`. Reserved: a custom
   client cannot serve its own content at this exact path.
2. Otherwise: `config.web_client` if set, else the bundled client
   (`active_webpath()`).
3. If a custom client is active and step 2 missed: the bundled client
   again, as a fallback.
4. `config.public_dirs` (unchanged from the no-`web_client` case).
5. 404.

**Why step 3 exists — this is the part worth internalizing, not just the
config key.** The bundled client's production build hardcodes root-relative
asset paths (`/js/<hash>.js`), not paths relative to wherever its HTML was
served from — a `<base>` tag doesn't change that, it only affects relative
URLs. So even while `/default`'s own `index.html` is what's on screen, the
browser still requests its JS/CSS from unprefixed `/js/...` paths. Without
step 3, those requests would miss against a custom client's root and
`/default` would render as an unstyled, broken shell whenever a
`web_client` is active. The same fallback is also what lets a minimal
custom client skip shipping its own favicon/fonts/icons.

`start()` calls `warn_if_web_client_misconfigured()` on every startup: a
`web_client` set but missing `index.html` at its root gets a console
warning, never a hard failure — every lookup against it just misses, so
`/` ends up fully served by the bundled client via step 3 until it's fixed.

Tests: `tests/core/test_web_client.py`.

---

## 16. Integral Autotesting

Two independent things happen around `unisi.start()`:

1. **Always, unconditionally** — every screen is structurally validated
   (`check_module`/`check_block` in `unisi/autotest.py`): duplicate
   element/block names, a non-`Block` inside `blocks`, a non-`Unit` inside a
   block, a `Chart` missing both `view` and `option`. Errors print to the
   console at startup even if you've never touched `config.autotest`. Read
   the console on first run of new/changed screens.
2. **Gated behind `config.autotest`**:
   - `True` / `'*'` — replay every fixture file already recorded in the
     `autotest/` directory and diff the actual response against the recorded
     one; a list of filenames restricts this to just those.
   - Also runs every function registered with `@unisi.test` once, at startup
     (sync or async).
   - Also adds an "Add test" toolbar button (see §11) that opens a dialog to
     name and record a new fixture: click it, interact with the screen, click
     the same button again to stop and write the JSON fixture to
     `autotest/<name>.json`.

```python
from unisi import test

@test
async def smoke_check():
    ...   # runs once at every startup when config.autotest is truthy
```

---

## 17. Common Mistakes (Verified Against Source)

| Mistake | Correct |
|---|---|
| `return [w1, w2]` to "send" widgets | unnecessary — auto-tracking sends them |
| `return update(w1, w2)` | `update()` does not exist in the API |
| Return a `Unit` to "update" the client | returning a `Unit` means ROLLBACK — the client reverts its own change |
| Call restore logic inside `prepare()` | not needed — UNISI restores persisted state before `prepare()` runs, every time |
| Assume module-level `global x` leaks across users | safe — each user gets their own fresh module execution |
| Assume a `blocks/` module is one instance shared by *every* user | it's shared across *one user's own* screens only; a different user gets a fully separate execution (§1) |
| Assume `isinstance(value, list)` in `table.changed` is dead code | wrong — `value` really is a `list` when multi-select is active |
| `persist=True` on a large, frequently-changing field (LLM output, JSON blob) | only that field's row gets rewritten each time, but it's still wasteful at scale — write to a file/DB, or snapshot on demand with `persist_units()` |
| Escape `{`/`}` before calling `Q()`/`Qx()` | not needed — untouched braces (JSON, code) pass through as-is; escaping doubles them incorrectly |
| Expect `Q()` to read local variables by name | it does not — pass every `{name}` placeholder explicitly as a kwarg, or it's silently left as literal text |
| `config.freeze_time = ...` | wrong attribute name — it's `config.froze_time`; the wrong name is silently ignored |
| `async def f(): ...` with no `await` inside | drop `async` — every caller's `await f()` should drop too |
| A blocking/CPU-heavy loop directly inside a handler | blocks the event loop for *every* connected user — use `await user.run_process(fn, *args)` (§7) |
| Assume `@handle(unit, 'changed')` replaces the existing handler | it composes with it — both run, in order, unless one returns `True`/`Redesign` (§5) |
| Assign `config.llm = 'openai'` (a bare string) | must be a `[provider, model]` list/tuple, or `['host', ...]` (§8) |
| Assume the persist DB schema has a `user_id` column | it doesn't — one SQLite file per session already provides that isolation (§9) |
| Assume a custom `web_client` must ship every bundled asset (favicon, fonts...) | it doesn't — anything it's missing transparently falls back to the bundled client (§15) |
| Try to serve custom content at `/default` | reserved — always resolves against the bundled client regardless of `config.web_client` (§15) |

---

## 18. Where to Go Next

- Building your first screen? → `unisi-quickstart.md`, then this file's §1–§4.
- Need every constructor option, exhaustively? → `unisi-programming-spec.md`
  (numbered §-sections referenced throughout this file).
- Doing anything with a DB-backed `Table`? → `persistent_tables.md` — links,
  schema evolution, geo-spatial fields, the full `Dbtable` API.
- Adding or debugging voice control? → `voicecom.md`.
- Working examples in this repo, by topic (all under `test_apps/`):
  - `test_apps/blocks/screens/main.py` — blocks, `Net`/`Graph`, toolbar, `@handle`
  - `test_apps/blocks/screens/zoo.py` — `ParamBlock`, `HTML`, pandas table
  - `test_apps/blocks/blocks/tblock.py` — dialogs, table hooks, autocomplete, tree
  - `test_apps/db/screens/single.py`, `linked.py` — persistent tables
  - `test_apps/llm/screens/main.py` — `Q()`/`Qx()`, per-unit `llm=`, `asyncio.gather`
  - `test_apps/persistence/screens/animals.py`, `notes.py` — positional + keyed persist
  - `test_apps/proxy/run_blocks.py`, `run_vision.py` — Remote API / `Proxy`
  - `tests/core/test_web_client.py` — `config.web_client`, `/default`, the bundled-client fallback (§15; not under `test_apps/` — it's a unit-test file, not a demo screen)
