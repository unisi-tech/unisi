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
  config.py (optional, auto)
  run.py
  screens/ (optional, auto)
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

message = TextArea("Message", "Ready")

def on_change(unit, new_value):
    if new_value < 0:
        return Error("Value must be >= 0", unit)
    unit.accept(new_value)

value = Edit("Value", 0, on_change)

async def run_action(*_):
    await user.progress("Processing...")
    #do something
    message.value = f"Current value: {value.value}"    

blocks = Block("Quick Start", [Button("Run", run_action)], value, message, icon="api")
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
blocks = Block("Second Screen", Text("Hello"))
```

UNISI auto-loads all `screens/*.py` modules and builds the menu.

## 5. Next Steps

1. Shared blocks and its data: create `blocks/` and import into screens.
2. Data tables: use `Table(...)` with `rows`/`headers`.
3. Persistent DB tables: set `db_dir` in `config.py`.
4. LLM fields and queries: set `llm` in `config.py`, then use `llm=...` and `Q(...)`

