# VoiceCom

Voice command module for the [UNISI](https://github.com/unisi-tech/unisi) framework.  
File: `unisi/voicecom.py`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture: State Machine](#2-architecture-state-machine)
3. [Quick Start](#3-quick-start)
4. [Command Vocabulary](#4-command-vocabulary)
5. [Modes](#5-modes)
   - [root — element selection](#51-root--element-selection)
   - [text — text field](#52-text--text-field)
   - [number — number field](#53-number--number-field)
   - [switch / check — toggle](#54-switch--check--toggle)
   - [select / list / radio / tree — option list](#55-select--list--radio--tree--option-list)
   - [graph / net — graph](#56-graph--net--graph)
   - [table — table](#57-table--table)
   - [command — button](#58-command--button)
   - [screen — screen navigation](#59-screen--screen-navigation)
6. [Global Escape Commands](#6-global-escape-commands)
7. [The Mate Panel](#7-the-mate-panel)
8. [VoiceCom Public API](#8-voicecom-public-api)
9. [Helper Functions](#9-helper-functions)
10. [Extending the Vocabulary](#10-extending-the-vocabulary)
11. [Usage Scenarios](#11-usage-scenarios)
12. [Internal Logic](#12-internal-logic)
13. [Known Limitations](#13-known-limitations)

---

## 1. Overview

`VoiceCom` is the voice controller for a UNISI user session. It receives individual words recognised by an STT engine and translates them into actions on the GUI elements of the current screen: filling fields, changing values, navigating between screens, and working with graphs and tables.

**Key principles:**

- One instance per user session.
- Works on top of a regular UNISI screen — no special screens or separate routing required.
- Words are matched fuzzily (Ratcliff/Obershelp via `SequenceMatcher`), so minor STT errors are corrected automatically.
- The system detects the type of the active element and switches mode accordingly.

---

## 2. Architecture: State Machine

`VoiceCom` operates as a finite state machine. The current state is stored in `self.mode`. Each incoming word is handled by the handler that corresponds to the current mode.

```
┌─────────────────────────────────────────────────────────────────┐
│                         ANY MODE                                │
│   Words: root / reset / select / cancel / screen / menu / stop  │
│   → always intercepted BEFORE the mode handler                  │
└────────────────────────────┬────────────────────────────────────┘
                             │
              ┌──────────────▼──────────────┐
              │           root              │  ← initial mode
              │  Fuzzy-match against names  │
              │  of elements on the screen  │
              └──────────┬──────────────────┘
                         │ activate_unit()
         ┌───────────────┼───────────────────────┐
         │               │                       │
    ┌────▼───┐      ┌────▼──────┐           ┌────▼────┐
    │  text  │      │  number   │           │ switch  │
    │        │      │           │           │  check  │
    │ insert │      │ set float │           │ select  │ ...
    │ words  │      │ value     │           │  list   │
    └────┬───┘      └────┬──────┘           │  radio  │
         │               │                  │  tree   │
         └───────────────┴──────────────────┴────┬────┘
                                                 │
                                     ┌───────────▼──────────┐
                                     │  graph / net / table │
                                     │  command / screen    │
                                     └──────────────────────┘
```

**Mode transitions:**

| From | Command | To |
|------|---------|-----|
| any | `root` / `reset` / `select` / `cancel` | `root` |
| any | `screen` / `menu` | `screen` |
| any | `stop` | Mate hidden |
| `root` | element name (fuzzy ≥ 0.8) | element's mode |
| `root` | `ok` + confirmation | element's mode |
| `screen` | screen name (fuzzy > 0.9) | `root` (new screen) |

---

## 3. Quick Start

### Initialisation

```python
# In the user session code (users.py)
from .voicecom import VoiceCom

class User:
    def __init__(self):
        ...
        self.voice = VoiceCom(self)

# Activate when a voice request arrives
async def process(self, request):
    if request.type == "voice":
        self.voice.start()          # show the Mate panel
        return await self.voice.process_string(request.data)
```

### Switching screens

```python
# When the user navigates to another screen
def set_screen(self, screen_name):
    self.screen = self.screens[screen_name]
    if hasattr(self, "voice"):
        self.voice.set_screen(self.screen)
```

### Stopping

```python
self.voice.stop()   # hide the Mate panel, state is preserved
```

---

## 4. Command Vocabulary

### Canonical commands

Every command has a canonical name and a list of synonyms. The system automatically maps synonyms to their canonical name during processing.

| Canonical name | Synonyms | Action |
|---------------|----------|--------|
| `root` | `select`, `choose`, `set` | Return to element selection |
| `reset` | `cancel` | Return to element selection |
| `screen` | `menu` | Go to screen selection |
| `stop` | — | Hide the Mate panel |
| `ok` | `okay` | Confirm a pending choice |
| `backspace` | `back` | Delete the character to the left of the cursor |
| `enter` | `push`, `execute`, `run` | Insert newline / press button |
| `clean` | `empty`, `erase` | Clear the field |
| `push` | `execute`, `run` | Press a command button |

### Processing priority

```
Incoming word
      │
      ▼
word2command.get(word)  →  canonicalised command
      │
      ├─ command in _ROOT_CMD_SET?  →  _handle_root_command()   ← ALWAYS
      │   {root, reset, screen, stop}                             first
      │
      └─ no → dispatch by self.mode
```

> **Important:** `root`, `reset`, `screen`, and `stop` are intercepted **before** the mode handler. They are the only guaranteed way to exit `text` or `number` mode.

---

## 5. Modes

### 5.1 root — element selection

**When active:** on startup, after `reset`, after finishing an edit.

**context_list shows:** all interactive elements on the current screen (sorted pretty4 names).

**Word processing algorithm:**

```
Word
  │
  ├─ command == "ok" AND context is set?
  │     → activate_unit(context)
  │
  ├─ another command?
  │     → _dispatch_context_command()
  │
  └─ plain word:
        buffer.append(word)
        fuzzy_match against unit_names
          similarity ≥ 0.8  → activate_unit immediately
          0 < sim < 0.8     → context = best_match, "Ok to confirm"
          sim == 0          → "Continue.." (accumulate buffer)
```

**Examples:**

```
User says:      Result:
──────────────  ──────────────────────────────────────
"email"         → activates the "Email address" field
"phon"          → context = "Phone number", "Ok to confirm"
"ok"            → activate_unit("Phone number")
"reset"         → stay in root, clear buffer
```

---

### 5.2 text — text field

**When active:** when an element with `unit.type == "string"` is selected.

**context_list shows:** all element names on the screen (for reference only; does not affect fuzzy matching of dictated words).

**Status message:** `"Dictate text. Say 'root' or 'reset' to switch element."`

#### Word insertion

Every spoken word is inserted into the field followed by a space. The cursor position `unit.x` is respected:

- `unit.x == -1` — append to the end.
- otherwise — insert at position `x` and shift the tail.

#### Double-tap — execute a command

If the last word in the buffer equals the new word AND it maps to a `text` command, the command is executed instead of being inserted:

```
"delete"   → inserts "delete " into the field
"delete"   → (repeat) executes the delete-character command
```

> The buffer is cleared after the command executes.

#### Editing commands

| Word | Synonym | Action |
|------|---------|--------|
| `left` | — | Move cursor left |
| `right` | — | Move cursor right |
| `up` | — | Move cursor to the start of the current line |
| `down` | — | Move cursor to the end of the current line |
| `backspace` | `back` | Delete character to the left |
| `delete` | — | Delete character to the right |
| `space` | — | Insert a space |
| `tab` | — | Insert a tab character |
| `enter` | `push`, `execute`, `run` | Insert a newline |
| `undo` | — | Restore the previous state |
| `clean` | `empty`, `erase` | Clear the entire field |

#### Global escape commands (intercepted before the mode handler)

| Word | Synonyms | Destination |
|------|---------|------------|
| `root` | `select`, `choose`, `set` | → `root` mode |
| `reset` | `cancel` | → `root` mode |
| `screen` | `menu` | → `screen` mode |
| `stop` | — | → hide Mate |

---

### 5.3 number — number field

**When active:** when an element with `unit.type == "range"` is selected.

**Algorithm:**

```
Word
  │
  ├─ command?  →  execute command (backspace, clean, undo, etc.)
  │
  └─ otherwise → word_to_number(word)
       number    → unit.value = float
       not number → "Not a number"
```

> Commands are checked **first**. Without this, `"backspace"` would be passed to the number converter and silently return `None`.

#### Commands

| Word | Action |
|------|--------|
| `left` | Move cursor left (character by character) |
| `right` | Move cursor right |
| `backspace` / `back` | Delete digit to the left |
| `delete` | Delete digit to the right |
| `undo` | Restore the previous value |
| `clean` / `empty` / `erase` | Clear (→ `None`) |

#### Number recognition

`word_to_number` accepts:
- Digit strings: `"42"`, `"3.14"`, `"1,000"` (commas are ignored).
- English number words (via `word2number`): `"forty two"`, `"one hundred"`.

---

### 5.4 switch / check — toggle

**When active:** when an element with `unit.type == "switch"` or `"check"` is selected.

**context_list:** `["true", "false", "yes", "no", "on", "off"]`

**Algorithm:**

```
Word → fuzzy_match against ["true","false","yes","no","on","off"]
  sim ≥ 0.8  → unit.value = (choice in ["true","yes","on"])
  otherwise  → context = choice, "Ok to confirm"
"ok"         → apply context
```

**Examples:**

```
"yes"   → unit.value = True
"off"   → unit.value = False
"tru"   → context = "true", "Ok to confirm"
"ok"    → unit.value = True
```

---

### 5.5 select / list / radio / tree — option list

**When active:** when an element of the corresponding type is selected.

**context_list:** `unit.options` — the list of available choices.

**Algorithm:** same as `switch`, but the value is assigned as a string:

```
Word → fuzzy_match against unit.options
  sim ≥ 0.8  → unit.value = choice
  otherwise  → context = choice, "Ok to confirm"
"ok"         → unit.value = context
```

**Examples:**

```
# unit.options = ["Small", "Medium", "Large", "Extra Large"]
"medium"   → unit.value = "Medium"
"larg"     → context = "Large", "Ok to confirm"
"ok"       → unit.value = "Large"
```

---

### 5.6 graph / net — graph

**When active:** when an element with `unit.type == "graph"` or `"net"` is selected.

**context_list:** labels of all nodes and edges in the format `"node:Label"` / `"edge:SRC-TGT"`.

#### Graph control commands

| Command | Action |
|---------|--------|
| `add` | Two-step: prompt for name → create `Node` |
| `remove` | Delete selected nodes and edges (null-marked) |
| `connect` | Two-step: prompt for target → create `Edge` |
| `disconnect` | Delete selected edges |
| `select` | Refresh context_list with current labels |
| `deselect` | `unit.value = {"nodes": [], "edges": []}` |
| `clear` | Requires `"ok"` to confirm → delete everything |
| `node` | Next word is interpreted as a node name to select |
| `edge` | Next word is interpreted as an edge label to select |

#### Two-step operations

```
"add"      → Mate: "Say new node name"
"alpha"    → creates Node("alpha")

"connect"  → Mate: "Say target node name"  (source = currently selected node)
"beta"     → creates Edge(source_id, target_id)

"node"     → Mate: "Say node name"
"alpha"    → selects node alpha
```

#### Fuzzy element search

A spoken word is matched against labels such as `"node:Alpha"` — the user can simply say `"alpha"` instead of the full label.

#### `unit.value` structure

```python
unit.value = {
    "nodes": [0, 3],    # indices of selected nodes
    "edges": [1],       # indices of selected edges
}
```

Deleted elements are marked `None` in the `unit.nodes` / `unit.edges` lists (UNISI convention).

---

### 5.7 table — table

**When active:** when an element with `unit.type == "table"` is selected.

**context_list:** column headers from `unit.headers`.

#### Navigation

| Command | Synonym | Action |
|---------|---------|--------|
| `next` | `down` | Next row |
| `prev` | `up` | Previous row |
| `right` | — | Next column |
| `left` | — | Previous column |
| `page` | — | Jump forward 10 rows |
| `row` | — | Select current row (`unit.value`), call `changed` |
| `column` | — | Announce the current column name |

#### Editing

| Command | Action |
|---------|--------|
| `edit` | Display the value of the current cell `(row, col)` |
| `confirm` / `enter` | Call `unit.update(unit, (row, col))` |
| `delete` / `backspace` | Call `unit.delete(unit, row)` |

#### Voice column selection

A spoken word is fuzzy-matched against `unit.headers`. At `similarity ≥ 0.8` the cursor moves to the matching column:

```
# headers = ["Name", "Email", "Department", "Salary"]
"depart"   → _table_col = 2 (Department)
"sala"     → _table_col = 3 (Salary)
```

#### Position announcement

After every navigation command, the `message` field is updated:
```
Row 3/50  Col 'Department' (3/4)
```

#### Expected element interface

```python
table_unit.headers  # list[str] — column headers
table_unit.rows     # list[list] — row data
table_unit.value    # int or list[int] — selected row(s)
table_unit.delete   # async handler(unit, row_index)
table_unit.update   # async handler(unit, (row, col))
table_unit.changed  # async handler(unit, command_str) — fallback
```

---

### 5.8 command — button

**When active:** when a button element (`unit.type == "command"`) is selected.

| Command | Synonyms | Action |
|---------|---------|--------|
| `push` | `execute`, `run`, `enter` | Call `unit.changed(unit, None)` |
| `ok` | `okay` | Same |

---

### 5.9 screen — screen navigation

**When active:** after the `screen` / `menu` command from any mode.

**context_list:** names of all screens except the current one.

**Algorithm:**

```
Word
  │
  ├─ command == "ok" AND context?  →  user.set_screen(context)
  │
  └─ buffer.append(word)
       fuzzy_match against screen_names
         sim > 0.9   → user.set_screen(match) immediately
         otherwise   → context = match, "Ok to confirm"
```

The threshold `0.9` (stricter than for elements) reduces the risk of accidentally switching screens.

---

## 6. Global Escape Commands

The following commands are intercepted **in `process_word` before any mode-specific handler**. They work from any mode, including `text` and `number`, and are never inserted into an input field.

| Word | Synonyms | Result |
|------|---------|--------|
| `root` | `select`, `choose`, `set` | `reset()` → `root` mode |
| `reset` | `cancel` | `reset()` → `root` mode |
| `screen` | `menu` | `set_mode("screen")` |
| `stop` | — | `stop()` → hide Mate |

> `ok` is **not** a global escape command: it has different meanings in different modes (confirm a choice, press a button). Only the four commands above act as strict escapes.

---

## 7. The Mate Panel

Calling `start()` appends a floating "Mate:" block to the current screen containing the following widgets:

```
┌─ Mate: ──────────────────────────────────────┐
│  System message  [status message text        ]│
│  Recognized words [last recognized word      ]│
│  Elements         ┌──────────────────────────┐│
│                   │ Email address             ││
│                   │ Phone number              ││
│                   │ First name                ││
│                   └──────────────────────────┘│
│  Commands         ┌──────────────────────────┐│
│                   │ root                      ││
│                   │ screen                    ││
│                   │ reset                     ││
│                   └──────────────────────────┘│
└──────────────────────────────────────────────┘
```

| Widget | Role |
|--------|------|
| **System message** | Current status / hint (read-only) |
| **Recognized words** | Last recognised word; editable field for manual input |
| **Elements** | Context list: elements / options / screens (depends on mode) |
| **Commands** | List of available commands; tap = execute command |

**Tapping an entry** in the Elements list calls `select_elem`, which activates the element or switches the screen.  
**Tapping a command** in the Commands list calls `select_command`, which forwards the command to `process_word`.

### Typical System message values

| Message | When |
|---------|------|
| `"Select an element or command"` | `root` mode |
| `"Dictate text. Say 'root' or 'reset' to switch element."` | `text` mode |
| `"Continue.."` | Accumulating buffer, no good match yet |
| `'"Ok" to confirm'` | Candidate found with sim < threshold |
| `"Not a number"` | Unrecognised word in `number` mode |
| `"Element not found"` | `activate_unit` received `None` |
| `"Select a screen"` | `screen` mode |
| `"Say new node name"` | Waiting for new node name in `graph` |
| `"Say target node name"` | Waiting for edge target in `graph` |
| `"Row N/M  Col 'X' (K/L)"` | After navigation in `table` |

---

## 8. VoiceCom Public API

### Constructor

```python
VoiceCom(user)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `user` | User | UNISI user session object |

Automatically calls `set_screen(user.screen)`.

---

### Lifecycle methods

#### `start() → None`

Shows the Mate panel on the current screen and calls `reset()`.

`screen.blocks` may be a tuple (stored as such by the UNISI Unit proxy). The method converts it to a list, appends the block, and writes it back.

```python
voice.start()
```

#### `stop() → None`

Hides the Mate panel. State is preserved — a subsequent `start()` resumes from the same point.

#### `reset() → None`

Switches to `root` mode, clears the buffer, and deactivates the current element. If an active dialog is present, shows the dialog's elements and adds `"close"` to the command list.

#### `set_screen(screen) → None`

Switches the tracked screen, re-indexes interactive elements, and calls `reset()`.

The order of operations inside the method is critical:

```python
def set_screen(self, screen):
    self.screen = screen            # 1. assign first
    self.calc_interactive_units()  # 2. then index
    self.reset()
```

`calc_interactive_units()` reads `self.screen`, **not** `self.user.screen`. This matters: at the moment `VoiceCom.set_screen()` is called, `self.user.screen` may still point to the old screen if the user session updates its own reference later. Using `self.screen` guarantees that the screen actually passed as the argument is the one being indexed.

```python
# When the user navigates:
voice.set_screen(user.screens["settings"])
```

---

### Input processing methods

#### `async process_string(string: str) → Any`

Splits the string on whitespace and processes each word sequentially via `process_word`.

```python
result = await voice.process_string("delete delete")
# first "delete"  → insert into the field
# second "delete" → execute the delete-character command (double-tap)
```

#### `async process_word(word: str) → Any`

Processes a single word. Returns the handler result (usually `None`, or the return value of a user-defined `changed` handler).

```python
result = await voice.process_word("reset")
```

---

### Properties

All three properties are thin wrappers over the state of the Mate panel widgets.

| Property | Type | Description |
|----------|------|-------------|
| `context_options` | `list` | Options shown in context_list |
| `commands` | `list` | Options shown in command_list |
| `context` | `Any` | Currently selected item in context_list |

---

## 9. Helper Functions

### `find_most_similar_sequence(input_string, string_list) → (str, float)`

Fuzzy-compares a string against a list of candidates. Returns the best match and its similarity ratio (0.0–1.0).

Uses `difflib.SequenceMatcher` (Ratcliff/Obershelp algorithm). Comparison is case-insensitive.

```python
match, ratio = find_most_similar_sequence("phon", ["Phone number", "Email", "Address"])
# → ("Phone number", 0.666...)

match, ratio = find_most_similar_sequence("email", ["Email address", "Phone"])
# → ("Email address", 0.769...)
```

**Thresholds used in VoiceCom:**

| Context | Threshold | Action |
|---------|-----------|--------|
| Element names, list options | ≥ 0.8 | Apply immediately |
| Element names, list options | < 0.8 | Show candidate, wait for `"ok"` |
| Screen names | > 0.9 | Switch immediately |
| Screen names | ≤ 0.9 | Show candidate, wait for `"ok"` |

---

### `word_to_number(word: str) → float | None`

Converts a string to a number. Tries in order:
1. `float()` directly (`"3.14"`, `"1000"`, `"1,000"`).
2. `word2number.w2n.word_to_num()` for number words (`"forty two"`, `"one hundred"`).
3. `None` if both attempts fail.

```python
word_to_number("42")           # → 42.0
word_to_number("3.14")         # → 3.14
word_to_number("1,500")        # → 1500.0
word_to_number("forty two")    # → 42.0
word_to_number("one hundred")  # → 100.0
word_to_number("hello")        # → None
```

---

## 10. Extending the Vocabulary

### Adding a synonym to an existing command

```python
# In voicecom.py, in the command_synonyms dict:
command_synonyms = dict(
    ...
    backspace=["back", "remove", "del"],  # added "del"
    ...
)
```

After the change, `word2command` and `ext_root_commands` must be rebuilt — they are constructed at import time, so the change takes effect after a restart.

### Adding a new command to a mode

```python
modes = dict(
    ...
    text=["left", "right", ..., "select_all"],  # added "select_all"
    ...
)
```

Add the handler inside `_text_command`:

```python
async def _text_command(self, u, command):
    ...
    case "select_all":
        u.value = u.value  # select all — logic depends on the frontend
    ...
```

### Localisation

All user input is English (dictation). System messages (`message.value`) can be localised without touching the core logic:

```python
# Example wrapper for French messages
class FrVoiceCom(VoiceCom):
    def reset(self):
        super().reset()
        self.message.value = "Sélectionnez un élément ou une commande"
```

---

## 11. Usage Scenarios

### Scenario 1: Filling a text form

```
User: "email"
  → Mate: context = "Email address", '"Ok" to confirm'

User: "ok"
  → activate_unit(Email field)
  → mode = "text"
  → Mate: "Dictate text. Say 'root' or 'reset' to switch element."

User: "john"
  → Email.value = "john "

User: "at"
  → Email.value = "john at "

User: "example"
  → Email.value = "john at example "

User: "root"
  → reset() → mode = "root"    ← global escape from text mode

User: "name"
  → context = "First name", '"Ok" to confirm'
```

### Scenario 2: Number field

```
User: "age"            → activates Age field (number mode)
User: "twenty five"   → unit.value = 25.0
User: "backspace"     → delete last digit → 2.0
User: "undo"          → restore 25.0
User: "clean"         → unit.value = None
User: "100"           → unit.value = 100.0
```

### Scenario 3: Option list

```
# unit.options = ["Manager", "Developer", "Designer", "QA Engineer"]
User: "role"          → activates Role field (select mode)
User: "developer"    → sim ≥ 0.8 → unit.value = "Developer"
User: "QA"           → sim < 0.8 → context = "QA Engineer"
User: "ok"           → unit.value = "QA Engineer"
```

### Scenario 4: Screen navigation

```
User: "screen"        → mode = "screen"
                         context_list = ["Main", "Settings", "Reports"]

User: "sett"         → sim < 0.9 → context = "Settings"
                         Mate: '"Ok" to confirm'

User: "ok"           → user.set_screen("Settings")
```

### Scenario 5: Working with a graph

```
User: "graph widget" → activates Graph unit (graph mode)
                         context_list: ["node:Alpha", "node:Beta", "edge:Alpha-Beta"]

User: "alpha"        → sim ≥ 0.8 → select node "Alpha"
                         unit.value = {"nodes": [0], "edges": []}

User: "connect"      → Mate: "Say target node name"
                         _graph_edge_source = 0

User: "beta"         → creates Edge(0, 1)
                         Mate: "Edge added"

User: "add"          → Mate: "Say new node name"
User: "gamma"        → creates Node("gamma")
                         context_list updated

User: "clear"        → Mate: "Say 'ok' to confirm clearing the graph"
User: "ok"           → all nodes and edges deleted
```

### Scenario 6: Working with a table

```
User: "employees"    → activates Employees table (table mode)
                         context_list = ["Name", "Email", "Dept", "Salary"]
                         Mate: "Row 1/50  Col 'Name' (1/4)"

User: "next"         → _table_row = 1
                         Mate: "Row 2/50  Col 'Name' (1/4)"

User: "salary"       → _table_col = 3
                         Mate: "Row 2/50  Col 'Salary' (4/4)"

User: "edit"         → Mate: "Cell: 75000.0 — dictate new value"

User: "delete"       → calls unit.delete(unit, 1)

User: "page"         → _table_row = 11
                         Mate: "Row 12/50  Col 'Salary' (4/4)"
```

---

## 12. Internal Logic

### Fuzzy buffer (`_buffer_suits_name`)

The buffer accumulates a sequence of words and matches them as a single phrase:

```
Word 1: "first"   → buffer = ["first"]         → match "first name"   0.62
Word 2: "name"    → buffer = ["first", "name"] → match "First name"   0.91 ✓
```

The buffer is cleared:
- on a successful match (similarity ≥ threshold),
- after any command executes,
- on `reset()`.

### Command cache (`cached_commands`)

The command list for each mode is built once and cached in `self.cached_commands`. This prevents mutation of the module-level `modes` dict on every `set_mode()` call.

### Data source in `calc_interactive_units`

The method reads exclusively from `self.screen`, not `self.user.screen`:

```python
# Correct — guaranteed to be the new screen:
self.screen_name = self.screen.name
for block in flatten(self.screen.blocks): ...

# Incorrect — may still be the old screen if user hasn't updated yet:
# self.screen_name = self.user.screen.name
# for block in flatten(self.user.screen.blocks): ...
```

This guards against a race condition: `set_screen(new_screen)` first writes `self.screen = screen` and only then calls `calc_interactive_units()`. If the method read from `user.screen`, there would be a window — depending on when the calling code in `users.py` updates its own reference — in which the old screen would be indexed.

**Post-condition invariant** — after `set_screen()` returns:
```
self.screen is screen
self.unit_names  == pretty4 names of elements on screen
self.name2unit   == {pretty4 name: Unit} for screen
self.screen_name == screen.name
```

### `screen.blocks` type

`screen.blocks` is stored as a tuple by the UNISI Unit proxy (screen attributes are defined as plain Python assignments in screen module files). The `start()` and `stop()` methods convert the tuple to a list, mutate it, and write it back:

```python
blocks = list(self.screen.blocks)
blocks.append(self.block)
self.screen.blocks = blocks
```

### Two-step graph operations

The `add` and `connect` operations require two words from the user. Intermediate state is stored in:

```python
self._graph_pending_action: str | None  # "add_node" | "add_edge_target" | "select_node" | ...
self._graph_edge_source: int | None     # source node index for connect
```

Both fields are reset to `None` by `reset()`.

---

## 13. Known Limitations

**English vocabulary only**  
`word2command`, synonyms, and number words (`word2number`) are designed for English. Adapting the vocabulary dictionaries is required for a non-English STT engine.

**No undo stack**  
`previous_unit_value_x` stores only one previous step. Multiple consecutive `undo` operations are not supported.

**No coordinate operations in graphs**  
Node positions cannot be set by voice — only create, delete, and connect. Positioning remains a mouse operation.

**STT engine not included**  
`VoiceCom` does not capture audio or perform speech recognition. It expects ready-made word strings from an external STT engine. Integration with a specific engine (Web Speech API, Whisper, etc.) is the responsibility of the calling code.

**No autocomplete in tables**  
The `unit.complete` handler (if defined on a table element) is not called from voice mode.
