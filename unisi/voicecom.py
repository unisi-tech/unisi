# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Voice command module for UNISI framework.

State machine overview
----------------------
mode = "root"
    Context list shows all interactive element names on the screen.
    Speaking a word fuzzy-matches against those names.
    Saying a root command (screen / stop / reset) runs it immediately.

mode = "text" | "number" | "switch" | "check" | "select" | "list" |
        "radio" | "tree" | "graph" | "net" | "table" | "command"
    Set by activate_unit() when the user selects an element.
    Root commands (root / reset / stop / screen) ALWAYS work as
    an escape hatch, regardless of the current mode.
"""

from difflib import SequenceMatcher
from typing import Any
from word2number import w2n
from .users import *
from .units import *
from .containers import Block

# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def find_most_similar_sequence(
    input_string: str,
    string_list: list[str],
) -> tuple[str, float]:
    """Return (best_match, ratio). Returns ("", 0.0) for an empty list."""
    best_match = ""
    highest_ratio = 0.0
    lower_input = input_string.lower()
    for candidate in string_list:
        ratio = SequenceMatcher(None, lower_input, candidate.lower()).ratio()
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_match = candidate
    return best_match, highest_ratio


def word_to_number(word: str) -> float | None:
    """Convert a spoken word or digit string to float; None on failure."""
    normalized = word.replace(",", "")
    try:
        return float(normalized)
    except ValueError:
        pass
    try:
        return float(w2n.word_to_num(normalized))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Command vocabulary  (built once at import time)
# ---------------------------------------------------------------------------

command_synonyms: dict[str, list[str]] = dict(
    root=["select", "choose", "set"],
    backspace=["back"],
    enter=["push", "execute", "run"],
    clean=["empty", "erase"],
    screen=["menu"],
    push=["execute", "run"],
    reset=["cancel"],
    ok=["okay"],
)

root_commands: list[str] = ["root", "screen", "stop", "reset", "ok"]
ext_root_commands: list[str] = root_commands[:]

modes: dict[str, list[str]] = dict(
    text=["left", "right", "up", "down", "backspace", "delete",
          "space", "tab", "enter", "undo", "clean"],
    number=["backspace", "delete", "undo", "clean"],
    graph=["node", "edge", "add", "remove", "connect", "disconnect",
           "select", "deselect", "clear"],
    net=["node", "edge", "add", "remove", "connect", "disconnect",
         "select", "deselect", "clear"],
    table=["page", "row", "column", "left", "right", "up", "down",
           "backspace", "delete", "next", "prev", "edit", "confirm"],
    command=["push"],
)

word2command: dict[str, str] = {}
for _cmd, _syns in command_synonyms.items():
    for _syn in _syns:
        word2command[_syn] = _cmd
    if _cmd in root_commands:
        ext_root_commands.extend(_syns)

word2command.update({c: c for c in root_commands})
for _mode_cmds in modes.values():
    word2command.update({c: c for c in _mode_cmds})

# Modes where only the exact canonical escape word works (not synonyms),
# so "select" isn't misread as an escape when it's a graph node name.
STRICT_MODES: frozenset[str] = frozenset(["graph", "net", "table", "text", "number"])

_ROOT_CMD_SET: frozenset[str] = frozenset(["root", "reset", "screen", "stop"])

# Pre-build sorted command lists per mode — one allocation at import time.
_mode_commands: dict[str, list[str]] = {}
for _mode, _cmds in modes.items():
    _all = list(_cmds) + root_commands
    _extra: list[str] = []
    for _c in _all:
        if _c in command_synonyms:
            _extra.extend(command_synonyms[_c])
    _mode_commands[_mode] = sorted(set(_all + _extra))
_root_mode_commands: list[str] = sorted(set(ext_root_commands))

# Map unit.type → voice mode string
_TYPE_TO_MODE: dict[str, str] = {"string": "text", "range": "number"}


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class VoiceCom:
    """Voice command controller for a UNISI user session."""

    def __init__(self, user) -> None:
        self.user = user
        self.unit: Unit | None = None
        self.mode: str = "root"
        self.buffer: list[str] = []
        self.previous_unit_value_x: tuple | None = None
        self._table_row: int = 0
        self._table_col: int = 0
        self._table_editing: bool = False
        self._graph_pending_action: str | None = None
        self._graph_edge_source: int | None = None

        self.block = self._build_assist_block(user)
        self.set_screen(user.screen)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_assist_block(self, user) -> Block:
        """Create the floating Mate helper block (per-user, never shared)."""
        self.input = Edit("Recognized words", "", update=self.keyboard_input)
        self.message = Edit("System message", "", edit=False)
        self.context_list = Select(
            "Elements", None, self.select_elem, type="list", width=250
        )
        self.command_list = Select(
            "Commands", None, self.select_command, type="list", width=250
        )
        block = Block(
            "Mate:",
            self.message,
            self.input,
            self.context_list,
            self.command_list,
            closable=True,
            icon="mic",
        )
        block.set_reactivity(user)
        return block

    # ------------------------------------------------------------------
    # Screen / unit management
    # ------------------------------------------------------------------

    def set_screen(self, screen) -> None:
        """Switch to a new screen and rebuild the interactive-element index."""
        self.screen = screen
        self._index_interactive_units()
        self.reset()

    def _index_interactive_units(self) -> None:
        """Index all editable leaf units on self.screen by their pretty name."""
        names: list[str] = []
        index: dict[str, Unit] = {}
        self.screen_name = self.screen.name
        for top_block in flatten(self.screen.blocks):
            self._collect_units(top_block, names, index)
        names.sort()
        self.unit_names = names
        self.name2unit = index

    def _collect_units(
        self,
        container,
        names: list[str],
        index: dict[str, Unit],
    ) -> None:
        """Recursively walk a Block and collect editable leaf Units."""
        if container is self.block:  # never index Mate's own widgets
            return
        children = getattr(container, "value", None)
        if children is None:
            return
        for elem in flatten(children):
            if elem is None:
                continue
            child_value = getattr(elem, "value", None)
            is_block = (
                isinstance(child_value, (list, tuple))
                and len(child_value) > 0
                and hasattr(child_value[0], "name")
            )
            if is_block:
                self._collect_units(elem, names, index)
            elif getattr(elem, "edit", True) and hasattr(elem, "name"):
                pretty_name = pretty4(elem.name)
                if pretty_name not in index:
                    index[pretty_name] = elem
                    names.append(pretty_name)

    def activate_unit(self, unit: Unit | None) -> None:
        """Deactivate the previous unit, activate the new one, enter its mode."""
        if unit is None:
            self.message.value = "Element not found"
            return

        if self.unit:
            self.unit.active = False
            self.unit.focus = False

        self.unit = unit
        unit.active = True
        unit.focus = True
        self.message.value = "Select a command"

        mode = _TYPE_TO_MODE.get(unit.type, unit.type)
        self.set_mode(mode)

        if mode in ("text", "number"):
            self.previous_unit_value_x = (
                getattr(unit, "value", None),
                getattr(unit, "x", 0),
            )

    def set_mode(self, mode: str) -> None:
        """Configure commands and context list for an interaction mode."""
        self.context_list.value = None
        self.mode = mode
        self.buffer = []
        self.previous_unit_value_x = None
        self._graph_pending_action = None
        self._graph_edge_source = None
        self._table_editing = False

        self.command_list.options = _mode_commands.get(mode, _root_mode_commands)
        self.input.value = mode
        self.message.value = "Continue.."

        match mode:
            case "switch" | "check":
                self.context_list.options = ["true", "false", "yes", "no", "on", "off"]
            case "select" | "list" | "radio" | "tree":
                self.context_list.options = list(getattr(self.unit, "options", []))
            case "screen":
                self.context_list.options = [
                    getattr(s, "name")
                    for s in self.user.screens
                    if hasattr(s, "name") and s.name != self.user.screen.name
                ]
                self.message.value = "Select a screen"
            case "graph" | "net":
                self._refresh_graph_context()
            case "table":
                self.context_list.options = list(getattr(self.unit, "headers", []))
                self.message.value = "Say a column name to edit, or: next / prev / delete"
            case "text":
                self.context_list.options = self.unit_names
                self.message.value = "Dictate text. Say 'root' or 'reset' to switch element."
            case _:
                self.context_list.options = []

        self.context_list.value = None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Add Mate block to this user's screen.blocks and enter root mode.

        screen.blocks belongs to this user's own screen_module — independent
        sessions never share it. Plain list mutation is correct here.
        User.delete() calls stop() on disconnect so Mate is cleanly removed
        even when the browser tab is closed without explicitly stopping voice.
        """
        blocks = list(self.screen.blocks)
        if self.block not in blocks:
            blocks.append(self.block)
            self.screen.blocks = blocks
            self.user.changed_units.add(self.screen)  # notify frontend
        self.reset()

    def stop(self) -> None:
        """Remove Mate block from screen.blocks."""
        blocks = list(self.screen.blocks)
        if self.block in blocks:
            blocks.remove(self.block)
            self.screen.blocks = blocks
            self.user.changed_units.add(self.screen)  # notify frontend

    def reset(self) -> None:
        """Return to root mode; clear all transient state."""
        self.buffer = []
        self.mode = "root"
        self._table_row = 0
        self._table_col = 0
        self._table_editing = False
        self._graph_pending_action = None
        self._graph_edge_source = None

        self._index_interactive_units()

        if dialog := getattr(self.user, "active_dialog", None):
            self.command_list.options = sorted(set(_root_mode_commands + ["close"]))
            self.context_list.options = sorted(u.name for u in flatten(dialog.value))
        else:
            self.command_list.options = _root_mode_commands
            self.context_list.options = self.unit_names

        if self.unit:
            self.unit.active = False
            self.unit.focus = False
            self.unit = None

        self.input.value = ""
        self.message.value = "Select an element or command"
        self.context_list.value = None

    # ------------------------------------------------------------------
    # Event handlers wired to widgets
    # ------------------------------------------------------------------

    async def keyboard_input(self, _, value: str):
        return await self.process_string(value)

    def select_elem(self, elem, value: str) -> None:
        """Called when the user taps an entry in the context_list widget."""
        elem.value = value
        if not value:
            return
        if self.mode == "screen":
            self.user.set_screen(value)
        else:
            unit = self.name2unit.get(value)
            if unit is not None:
                self.activate_unit(unit)
            else:
                self.message.value = f"Element '{value}' not found"

    async def select_command(self, _, value: str):
        _.value = None
        return await self.process_word(value)

    # ------------------------------------------------------------------
    # Word / string processing
    # ------------------------------------------------------------------

    async def process_string(self, string: str) -> Any:
        """Split into words and process each; stop after a screen change."""
        screen_changed = None
        for word in string.split():
            if word and not screen_changed:
                screen_changed = await self.process_word(word)
        return screen_changed

    async def process_word(self, word: str) -> Any:
        """Route a single spoken word to the handler for the current mode."""
        self.input.value = word
        self.message.value = ""
        if not word:
            return None

        command = word2command.get(word) or word2command.get(word.lower())

        # Root-level navigation commands are global escapes.
        # In STRICT_MODES only canonical words trigger escape (not synonyms),
        # so "select" stays available as a node name in graph mode.
        if command in _ROOT_CMD_SET:
            if self.mode not in STRICT_MODES or word.lower() in _ROOT_CMD_SET:
                return await self._handle_root_command(command)

        match self.mode:
            case "text":
                return await self._process_text_mode(word, command)
            case "number":
                return await self._process_number_mode(word, command)
            case "switch" | "check" | "select" | "list" | "radio" | "tree":
                return await self._process_choice_mode(word, command)
            case "root":
                return await self._process_root_mode(word, command)
            case "screen":
                return await self._process_screen_mode(word, command)
            case "graph" | "net":
                return await self._process_graph_word(word, command)
            case "table":
                return await self._process_table_word(word, command)
            case _:
                if command:
                    return await self._dispatch_context_command(command)
                self.message.value = "Unknown command."
        return None

    # ------------------------------------------------------------------
    # Root-command handler (always reachable)
    # ------------------------------------------------------------------

    async def _handle_root_command(self, command: str) -> Any:
        self.buffer = []
        self.message.value = ""
        match command:
            case "root" | "reset":
                self.reset()
            case "screen":
                self.set_mode("screen")
            case "stop":
                self.stop()
        return None

    # ------------------------------------------------------------------
    # Text mode
    # ------------------------------------------------------------------

    async def _process_text_mode(self, word: str, command: str | None) -> Any:
        value = getattr(self.unit, "value", "") or ""
        ux = getattr(self.unit, "x", len(value))

        # Double-tap: same word spoken twice in a row executes the command
        if (command and command in set(modes["text"])
                and self.buffer and self.buffer[-1] == word):
            self.buffer = []
            if self.previous_unit_value_x:
                self.unit.value, self.unit.x = self.previous_unit_value_x
                self.previous_unit_value_x = None
            return await self._text_command(self.unit, command)

        self.previous_unit_value_x = value, ux
        self.buffer = [word]
        padded = word + " "
        if ux == -1:
            self.unit.value = (value + " " + word) if value else word
            self.unit.x = len(self.unit.value)
        else:
            self.unit.value = value[:ux] + padded + value[ux:]
            self.unit.x = ux + len(padded)
        return None

    async def _text_command(self, u: Unit, command: str) -> Any:
        value = getattr(u, "value", "") or ""
        ux = getattr(u, "x", len(value))
        match command:
            case "left":
                u.x = max(0, ux - 1)
            case "right":
                u.x = min(len(value), ux + 1)
            case "up":
                line_start = value.rfind("\n", 0, ux)
                u.x = 0 if line_start == -1 else line_start
            case "down":
                line_end = value.find("\n", ux)
                u.x = len(value) if line_end == -1 else line_end + 1
            case "backspace":
                if ux > 0:
                    u.value = value[: ux - 1] + value[ux:]
                    u.x = ux - 1
            case "delete":
                if ux < len(value):
                    u.value = value[:ux] + value[ux + 1:]
            case "space":
                u.value = value[:ux] + " " + value[ux:]
                u.x = ux + 1
            case "tab":
                u.value = value[:ux] + "\t" + value[ux:]
                u.x = ux + 1
            case "enter":
                u.value = value[:ux] + "\n" + value[ux:]
                u.x = ux + 1
            case "undo":
                if self.previous_unit_value_x:
                    u.value, u.x = self.previous_unit_value_x
            case "clean":
                u.value = ""
                u.x = 0
            case _:
                self.message.value = "Command is outside context"
        return None

    # ------------------------------------------------------------------
    # Number mode
    # ------------------------------------------------------------------

    async def _process_number_mode(self, word: str, command: str | None) -> Any:
        if command:
            self.buffer = []
            return await self._number_command(self.unit, command)

        num = word_to_number(word)
        if num is not None:
            self.previous_unit_value_x = (
                getattr(self.unit, "value", None),
                getattr(self.unit, "x", 0),
            )
            self.unit.value = num
        else:
            self.message.value = "Not a number"
        return None

    async def _number_command(self, u: Unit, command: str) -> Any:
        svalue = str(getattr(u, "value", "")) if getattr(u, "value", None) is not None else ""
        ux = getattr(u, "x", len(svalue))
        match command:
            case "left":
                u.x = max(0, ux - 1)
            case "right":
                u.x = min(len(svalue), ux + 1)
            case "backspace":
                if ux > 0:
                    raw = svalue[: ux - 1] + svalue[ux:]
                    try:
                        u.value = float(raw) if raw and raw != "-" else None
                    except ValueError:
                        u.value = None
                    u.x = ux - 1
            case "delete":
                if ux < len(svalue):
                    raw = svalue[:ux] + svalue[ux + 1:]
                    try:
                        u.value = float(raw) if raw and raw != "-" else None
                    except ValueError:
                        u.value = None
            case "undo":
                if self.previous_unit_value_x:
                    u.value, u.x = self.previous_unit_value_x
            case "clean":
                u.value = None
                u.x = 0
            case _:
                self.message.value = "Command is outside context"
        return None

    # ------------------------------------------------------------------
    # Choice mode  (switch / check / select / list / radio / tree)
    # ------------------------------------------------------------------

    async def _process_choice_mode(self, word: str, command: str | None) -> Any:
        if command == "ok":
            if self.context_list.value:
                self.message.value = ""
                self.buffer = []
                self._apply_choice(self.context_list.value)
            else:
                self.message.value = "Nothing to confirm"
            return None

        if command:
            return await self._dispatch_context_command(command)

        choice, similarity = self._buffer_suits_name(word)
        if similarity >= 0.8:
            self.buffer = []
            self._apply_choice(choice)
            self.message.value = ""
        elif choice:
            self.context_list.value = choice
            self.message.value = '"Ok" to confirm'
        else:
            self.command_list.options = _mode_commands.get(self.mode, _root_mode_commands)
            self.message.value = "Continue.."
            self.buffer = []
            self.input.value = ""
        return None

    def _apply_choice(self, choice: str) -> None:
        if self.mode == "switch":
            self.unit.value = choice in ("true", "yes", "on")
        else:
            self.unit.value = choice

    # ------------------------------------------------------------------
    # Root mode  (element selection)
    # ------------------------------------------------------------------

    async def _process_root_mode(self, word: str, command: str | None) -> Any:
        if command == "ok":
            if self.context_list.value:
                unit = self.name2unit.get(self.context_list.value)
                self.context_list.value = None
                if unit is not None:
                    self.activate_unit(unit)
                else:
                    self.message.value = "Element not found"
            else:
                self.message.value = "Nothing to confirm"
            return None

        if command:
            return await self._dispatch_context_command(command)

        unit_name, similarity = self._buffer_suits_name(word)
        if similarity >= 0.8:
            self.buffer = []
            unit = self.name2unit.get(unit_name)
            if unit is not None:
                self.activate_unit(unit)
            else:
                self.message.value = "Element not found"
        elif unit_name:
            self.context_list.value = unit_name
            self.message.value = '"Ok" to confirm'
        else:
            self.command_list.options = _root_mode_commands
            self.message.value = "Continue.."
            self.input.value = " ".join(self.buffer)
        return None

    # ------------------------------------------------------------------
    # Screen mode
    # ------------------------------------------------------------------

    async def _process_screen_mode(self, word: str, command: str | None) -> Any:
        if command == "ok":
            if self.context_list.value:
                self.user.set_screen(self.context_list.value)
            else:
                self.message.value = "Nothing to confirm"
            return None

        screen_name, similarity = self._buffer_suits_name(word)
        if similarity > 0.9:
            self.buffer = []
            self.user.set_screen(screen_name)
        elif screen_name:
            self.context_list.value = screen_name
            self.message.value = '"Ok" to confirm'
        return None

    # ------------------------------------------------------------------
    # Shared command dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_context_command(self, command: str) -> Any:
        if self.unit is None:
            self.message.value = "Command is out of context."
            return None

        u = self.unit
        match self.mode:
            case "text":
                self.buffer = []
                return await self._text_command(u, command)
            case "number":
                self.buffer = []
                return await self._number_command(u, command)
            case "graph" | "net":
                return await self._graph_command(u, command)
            case "table":
                return await self._table_command(u, command)
            case "switch" | "check" | "select" | "list" | "radio" | "tree":
                if command in ("push", "enter"):
                    handler = getattr(u, "changed", None)
                    if handler:
                        return await call_anysync(handler, u, u.value)
                self.message.value = "Command is outside context"
            case "command":
                if command in ("ok", "push", "enter"):
                    handler = getattr(u, "changed", None)
                    if handler:
                        return await call_anysync(handler, u, None)
                self.message.value = "Command is outside context"
        return None

    # ------------------------------------------------------------------
    # Graph / Net mode
    # ------------------------------------------------------------------

    def _graph_nodes(self) -> list:
        return getattr(self.unit, "nodes", None) or getattr(self.unit, "_nodes", [])

    def _graph_edges(self) -> list:
        return getattr(self.unit, "edges", None) or getattr(self.unit, "_edges", [])

    def _refresh_graph_context(self) -> None:
        if self.unit is None:
            self.context_list.options = []
            self.message.value = "No graph selected"
            return
        node_map: dict[str, int] = {}
        edge_map: dict[str, int] = {}
        labels: list[str] = []

        for i, node in enumerate(self._graph_nodes()):
            if name := getattr(node, "name", None):
                node_map[name] = i
                labels.append(name)

        for i, edge in enumerate(self._graph_edges()):
            if name := getattr(edge, "name", None):
                edge_map[name] = i
                labels.append(name)

        self._graph_node_map = node_map
        self._graph_edge_map = edge_map
        self.context_list.options = sorted(labels)
        self.message.value = (
            f"{len(labels)} elements. Say a name to select, or: add / remove / connect / disconnect"
        )

    async def _process_graph_word(self, word: str, command: str | None) -> Any:
        u = self.unit
        if u is None:
            self.message.value = "No graph active"
            return None

        if command == "ok":
            if self.context_list.value:
                self._select_graph_element_by_name(u, self.context_list.value)
                self.context_list.value = None
                self.buffer = []
            else:
                self.message.value = "Nothing to confirm"
            return None

        if command:
            return await self._dispatch_context_command(command)

        if self._graph_pending_action == "add_node":
            return self._graph_add_node(u, word)
        if self._graph_pending_action == "add_edge_target":
            return self._graph_connect(u, word)

        choice, sim = self._buffer_suits_name(word)
        if sim >= 0.8 and choice:
            self.buffer = []
            self.context_list.value = None
            self._select_graph_element_by_name(u, choice)
        elif choice:
            self.context_list.value = choice
            self.message.value = f'"{choice}" — say ok to confirm'
        else:
            self.message.value = "Element not found"
        return None

    # net mode is identical to graph mode
    _process_net_word = _process_graph_word

    def _select_graph_element_by_name(self, u: Unit, name: str) -> None:
        node_map = getattr(self, "_graph_node_map", {})
        edge_map = getattr(self, "_graph_edge_map", {})
        if name in node_map:
            u.value = {"nodes": [node_map[name]], "edges": []}
            self.message.value = f"Node '{name}' selected"
        elif name in edge_map:
            u.value = {"nodes": [], "edges": [edge_map[name]]}
            self.message.value = f"Edge '{name}' selected"
        else:
            self.message.value = f"'{name}' not found"

    async def _graph_command(self, u: Unit, command: str) -> Any:
        match command:
            case "add":
                self._graph_pending_action = "add_node"
                self.message.value = "Say new node name"
            case "remove":
                self._graph_remove_selected(u)
            case "connect":
                selected = (getattr(u, "value", {}) or {}).get("nodes", [])
                if not selected:
                    self.message.value = "Select a source node first"
                else:
                    self._graph_edge_source = selected[0]
                    self._graph_pending_action = "add_edge_target"
                    self.message.value = "Say target node name"
            case "disconnect":
                self._graph_remove_selected_edges(u)
            case "deselect":
                u.value = {"nodes": [], "edges": []}
                self.message.value = "Selection cleared"
            case _:
                self.message.value = "Unknown graph command"
        return None

    def _graph_add_node(self, u: Unit, name: str) -> None:
        from .graphs import Node
        nodes = self._graph_nodes()
        if not nodes:
            self.message.value = "Graph has no nodes list"
            self._graph_pending_action = None
            return
        nodes.append(Node(name))
        self._graph_pending_action = None
        self._refresh_graph_context()
        self.message.value = f"Node '{name}' added"
        self.user.changed_units.add(u)

    def _graph_connect(self, u: Unit, target_word: str) -> None:
        if self._graph_edge_source is None:
            self.message.value = "No source node selected"
            self._graph_pending_action = None
            return
        node_map = getattr(self, "_graph_node_map", {})
        target_id = node_map.get(target_word)
        if target_id is None:
            match, sim = find_most_similar_sequence(target_word, list(node_map))
            if sim >= 0.8:
                target_id = node_map[match]
        if target_id is None:
            self.message.value = f"Node '{target_word}' not found"
            return
        from .graphs import Edge
        edges = self._graph_edges()
        if not edges:
            self.message.value = "Graph has no edges list"
            self._graph_pending_action = None
            return
        edges.append(Edge(self._graph_edge_source, target_id))
        self._graph_pending_action = None
        self._graph_edge_source = None
        self._refresh_graph_context()
        self.message.value = "Edge added"
        self.user.changed_units.add(u)

    def _graph_remove_selected(self, u: Unit) -> None:
        val = getattr(u, "value", {}) or {}
        nodes = self._graph_nodes()
        edges = self._graph_edges()
        for nid in sorted(val.get("nodes", []), reverse=True):
            if 0 <= nid < len(nodes):
                nodes[nid] = None
        for eid in sorted(val.get("edges", []), reverse=True):
            if 0 <= eid < len(edges):
                edges[eid] = None
        u.value = {"nodes": [], "edges": []}
        self._refresh_graph_context()
        self.message.value = "Selected elements removed"
        self.user.changed_units.add(u)

    def _graph_remove_selected_edges(self, u: Unit) -> None:
        val = getattr(u, "value", {}) or {}
        edges = self._graph_edges()
        for eid in sorted(val.get("edges", []), reverse=True):
            if 0 <= eid < len(edges):
                edges[eid] = None
        u.value = {"nodes": val.get("nodes", []), "edges": []}
        self._refresh_graph_context()
        self.message.value = "Selected edges removed"
        self.user.changed_units.add(u)

    # ------------------------------------------------------------------
    # Table mode
    # ------------------------------------------------------------------

    async def _process_table_word(self, word: str, command: str | None) -> Any:
        if self.unit is None:
            self.message.value = "No table active"
            return None
        if command:
            return await self._dispatch_context_command(command)

        # If we're in cell-edit mode, the spoken word is the new cell value
        if self._table_editing:
            rows = getattr(self.unit, "rows", [])
            if rows and self._table_row < len(rows):
                row = rows[self._table_row]
                if self._table_col < len(row):
                    row[self._table_col] = word
                    self.user.changed_units.add(self.unit)
                    self._table_editing = False
                    self.message.value = f"Cell updated to {word!r}"
                    return None
            self.message.value = "Cannot write: row/column out of bounds"
            self._table_editing = False
            return None

        headers = getattr(self.unit, "headers", [])
        if headers:
            choice, sim = self._buffer_suits_name(word)
            if sim >= 0.8 and choice in headers:
                self.buffer = []
                self._table_col = headers.index(choice)
                rows = getattr(self.unit, "rows", [])
                if rows and self._table_row < len(rows):
                    row = rows[self._table_row]
                    cell = row[self._table_col] if self._table_col < len(row) else ""
                    self.message.value = (
                        f"Col '{choice}', row {self._table_row + 1}. "
                        f"Current: {cell!r}. Say 'edit' to change, or navigate."
                    )
                else:
                    self._announce_table_position()
                return None
        self.message.value = "Unknown table command"
        return None

    def _set_table_row(self, u: Unit, row: int) -> None:
        """Write the new selected row index into u.value (int or list[int])."""
        self._table_row = row
        val = getattr(u, "value", None)
        u.value = [row] if isinstance(val, list) else row

    async def _table_command(self, u: Unit, command: str) -> Any:
        rows = getattr(u, "rows", [])
        headers = getattr(u, "headers", [])
        total_rows = len(rows)
        total_cols = len(headers)

        match command:
            case "next" | "down":
                self._set_table_row(u, min(self._table_row + 1, total_rows - 1))
                self._announce_table_position()
            case "prev" | "up":
                self._set_table_row(u, max(self._table_row - 1, 0))
                self._announce_table_position()
            case "right":
                if self._table_col < total_cols - 1:
                    self._table_col += 1
                self._announce_table_position()
            case "left":
                if self._table_col > 0:
                    self._table_col -= 1
                self._announce_table_position()
            case "page":
                self._set_table_row(u, min(self._table_row + 10, max(total_rows - 1, 0)))
                self._announce_table_position()
            case "row":
                self._set_table_row(u, self._table_row)
                handler = getattr(u, "changed", None)
                if handler:
                    return await call_anysync(handler, u, self._table_row)
                self._announce_table_position()
            case "column":
                col_name = headers[self._table_col] if self._table_col < total_cols else "?"
                self.message.value = f"Column: {col_name}"
            case "delete" | "backspace":
                handler = getattr(u, "delete", None)
                if handler:
                    return await call_anysync(handler, u, self._table_row)
                self.message.value = "Delete not configured"
            case "edit":
                if rows and self._table_row < total_rows:
                    row = rows[self._table_row]
                    cell_val = row[self._table_col] if self._table_col < len(row) else "?"
                    self._table_editing = True
                    self.message.value = f"Cell: {cell_val!r} — dictate new value"
                else:
                    self.message.value = "No cell selected"
            case "confirm" | "enter":
                handler = getattr(u, "update", None)
                if handler:
                    return await call_anysync(handler, u, (self._table_row, self._table_col))
                self.message.value = "No update handler configured"
            case _:
                handler = getattr(u, "changed", None)
                if handler:
                    return await call_anysync(handler, u, command)
                self.message.value = "Unknown table command"
        return None

    def _announce_table_position(self) -> None:
        rows = getattr(self.unit, "rows", [])
        headers = getattr(self.unit, "headers", [])
        col_name = headers[self._table_col] if self._table_col < len(headers) else "?"
        self.message.value = (
            f"Row {self._table_row + 1}/{len(rows)}  "
            f"Col '{col_name}' ({self._table_col + 1}/{len(headers)})"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _buffer_suits_name(self, word: str) -> tuple[str, float]:
        """Append word to buffer and fuzzy-match the accumulated phrase."""
        self.buffer.append(word)
        name = " ".join(self.buffer)
        return find_most_similar_sequence(name, self.context_list.options)