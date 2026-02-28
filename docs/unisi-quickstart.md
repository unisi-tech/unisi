# UNISI Quick Start

This is a minimal path to your first working UNISI app.

For full API/spec details, see:
- [UNISI Programming Spec](./unisi-programming-spec.md)

## 1. Install

```bash
pip install unisi
```

## 2. Create Project Files

Create this structure:

```text
your_app/
  config.py
  run.py
  screens/
    main.py
```

### `config.py`

```python
port = 8000
appname = "UNISI Demo"
hot_reload = True
upload_dir = "web"
```

### `run.py`

```python
import unisi
unisi.start()
```

### `screens/main.py`

```python
from unisi import *

name = "Main"
order = 0

message = TextArea("Message", "Ready")
value = Edit("Value", 0)

def on_change(unit, new_value):
    if new_value < 0:
        return Error("Value must be >= 0", unit)
    return unit.accept(new_value)

value.changed = on_change

async def run_action(*_):
    await user.progress("Processing...")
    message.value = f"Current value: {value.value}"
    return message

block = Block("Quick Start", [Button("Run", run_action)], value, message, icon="api")
blocks = [block]
```

## 3. Run

From project root:

```bash
python run.py
```

Then open:

```text
http://localhost:8000
```

## 4. Add More Screens

Add more files in `screens/`:

```python
# screens/second.py
from unisi import *
name = "Second"
order = 1
blocks = [Block("Second Screen", Text("Hello"))]
```

UNISI auto-loads all `screens/*.py` modules and builds the menu.

## 5. Next Steps

1. Reusable blocks: create `blocks/` and import into screens.
2. Data tables: use `Table(...)` with `rows`/`headers`.
3. Persistent DB tables: set `db_dir` in `config.py`.
4. LLM fields and queries: set `llm` in `config.py`, then use `llm=...` and `Q(...)`.
5. Route integration: pass `http_handlers=[...]` into `unisi.start(...)`.

