# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Voice command module for UNISI framework.

Provides voice-driven interaction with GUI elements: text input, number input,
selection, screen navigation, graph/network manipulation and table operations.
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

def find_most_similar_sequence(input_string: str, string_list: list[str]) -> tuple[str, float]:
    """Return the best-matching string and its SequenceMatcher ratio."""
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
    """Convert a spoken word or numeric string to float; return None on failure.

    Uses ValueError (not bare except) for float(), and Exception for w2n
    because w2n raises various error types depending on version.
    """
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
# Command vocabulary
# ---------------------------------------------------------------------------

# Maps synonym words → canonical command names
command_synonyms: dict[str, list[str]] = dict(
    value=["is", "equals"],
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
    text=["text", "left", "right", "up", "down", "backspace", "delete",
          "space", "tab", "enter", "undo", "clean"],
    number=["number", "backspace", "delete", "undo", "clean"],
    graph=["node", "edge", "add", "remove", "connect", "disconnect",
           "select", "deselect", "clear"],
    net=["node", "edge", "add", "remove", "connect", "disconnect",
         "select", "deselect", "clear"],
    table=["page", "row", "column", "left", "right", "up", "down",
           "backspace", "delete", "next", "prev", "edit", "confirm"],
    command=["push"],
)

# Build a flat reverse-lookup: word → canonical command
word2command: dict[str, str] = {}
for _command, _synonyms in command_synonyms.items():
    for _syn in _synonyms:
        word2command[_syn] = _command
    if _command in root_commands:
        ext_root_commands.extend(_synonyms)

word2command.update({c: c for c in root_commands})
for _mode_cmds in modes.values():
    word2command.update({c: c for c in _mode_cmds})


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class VoiceCom:
    """
    Voice command controller for a UNISI user session.

    Listens to recognised words, interprets them as navigation commands,
    element selections, or value changes, and applies them to the active GUI.
    """

    def __init__(self, user) -> None:
        self.user = user
        self.unit: Unit | None = None
        self.mode: str = "root"
        self.buffer: list[str] = []
        self.previous_unit_value_x: tuple | None = None
        self.cached_commands: dict[str, list[str]] = {}
        # Table cursor state
        self._table_row: int = 0
        self._table_col: int = 0
        # Graph pending two-step state
        self._graph_pending_action: str | None = None
        self._graph_edge_source: int | None = None

        self.block = self.assist_block(user)
        self.set_screen(user.screen)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def assist_block(self, user) -> Block:
        """Create the floating "Mate" helper block shown during voice mode."""
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
    # Properties
    # ------------------------------------------------------------------

    @property
    def context_options(self) -> list:
        return self.context_list.options

    @context_options.setter
    def context_options(self, names: list) -> None:
        self.context_list.options = names

    @property
    def commands(self) -> list:
        return self.command_list.options

    @commands.setter
    def commands(self, commands: list) -> None:
        self.command_list.options = commands

    @property
    def context(self):
        return self.context_list.value

    @context.setter
    def context(self, value) -> None:
        self.context_list.value = value

    # ------------------------------------------------------------------
    # Screen / unit management
    # ------------------------------------------------------------------

    def set_screen(self, screen) -> None:
        """Switch to a new screen and rebuild the interactive-element index."""
        self.calc_interactive_units()
        self.screen = screen
        self.reset()

    def calc_interactive_units(self) -> None:
        """Index all editable units on the current screen by their pretty name."""
        interactive_names: list[str] = []
        name2unit: dict[str, Unit] = {}
        # FIX: was self.sreen_name (typo)
        self.screen_name = self.user.screen.name
        for block in flatten(self.user.screen.blocks):
            for elem in flatten(block.value):
                if getattr(elem, "edit", True):
                    # pretty4 is guaranteed by `from .units import *`
                    pretty_name = pretty4(elem.name)
                    name2unit[pretty_name] = elem
                    interactive_names.append(pretty_name)
        interactive_names.sort()
        self.unit_names = interactive_names
        self.name2unit = name2unit

    def activate_unit(self, unit: Unit | None) -> None:
        """Deactivate the previous unit, activate the new one, enter its mode."""
        if self.unit:
            self.unit.active = False
            self.unit.focus = False
        self.unit = unit
        self.message.value = "Select a command"
        if unit:
            match unit.type:
                case "string":
                    mode = "text"
                case "range":
                    mode = "number"
                case _:
                    mode = unit.type
            unit.active = True
            unit.focus = True
            self.set_mode(mode)
            if unit.type in ("text", "number"):
                self.previous_unit_value_x = (
                    getattr(unit, "value", None),
                    getattr(unit, "x", 0),
                )
        else:
            self.commands = ext_root_commands

    def set_mode(self, mode: str) -> None:
        """Configure UI for a specific interaction mode."""
        self.context = None
        self.mode = mode
        self.buffer = []
        self.previous_unit_value_x = None
        self._graph_pending_action = None
        self._graph_edge_source = None

        if mode not in self.cached_commands:
            # FIX: copy the list – do NOT mutate modes[mode]
            cmds = list(modes.get(mode, [])) + root_commands
            extra: list[str] = []
            for cmd in cmds:
                if cmd in command_synonyms:
                    extra.extend(command_synonyms[cmd])
            cmds.extend(extra)
            # FIX: set() removes duplicates that arise from synonym overlap
            cmds = sorted(set(cmds))
            self.cached_commands[mode] = cmds
        self.commands = self.cached_commands[mode]

        self.input.value = mode
        self.message.value = "Continue.."

        match mode:
            case "switch" | "check":
                self.context_options = ["true", "false", "yes", "no", "on", "off"]
            case "select" | "list" | "radio":
                self.context_options = list(getattr(self.unit, "options", []))
            case "tree":
                self.context_options = list(getattr(self.unit, "options", []))
            case "screen":
                self.context_options = [
                    getattr(s, "name")
                    for s in self.user.screens
                    if hasattr(s, "name") and s.name != self.user.screen.name
                ]
                self.message.value = "Select a screen"
            case "graph" | "net":
                self._refresh_graph_context()
            case _:
                self.context_list.options = []
        self.context = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Show the Mate block on the current screen.

        screen.blocks is stored as a tuple by the UNISI Unit proxy
        (__getattribute__ in units.py returns the raw value, which may be
        a tuple when blocks are defined as a plain assignment in a screen
        module, e.g. `blocks = block` or `blocks = b1, b2`).
        We must convert to list before mutating and write the result back.
        """
        blocks = list(self.screen.blocks)
        if self.block not in blocks:
            blocks.append(self.block)
            self.screen.blocks = blocks
        self.reset()

    def stop(self) -> None:
        """Hide the Mate block from the current screen."""
        blocks = list(self.screen.blocks)
        if self.block in blocks:
            blocks.remove(self.block)
            self.screen.blocks = blocks

    def reset(self) -> None:
        """Return to root mode; clear selection and buffers."""
        self.buffer = []
        self.mode = "root"
        self._table_row = 0
        self._table_col = 0
        self._graph_pending_action = None
        self._graph_edge_source = None

        if dialog := getattr(self.user, "active_dialog", None):
            cmds = sorted(set(ext_root_commands + ["close"]))
            # FIX: original had `commands.sort` (no parens → no-op)
            self.commands = cmds
            options = sorted(u.name for u in flatten(dialog.value))
            self.context_options = options
        else:
            self.commands = ext_root_commands
            self.context_options = getattr(self, "unit_names", [])

        if self.unit:
            self.unit.active = False
            self.unit.focus = False
            self.unit = None

        self.input.value = ""
        self.message.value = "Select a command or element"
        self.context = None

    # ------------------------------------------------------------------
    # Event handlers wired to widgets
    # ------------------------------------------------------------------

    async def keyboard_input(self, _, value):
        return await self.process_string(value)

    def select_elem(self, elem, value) -> None:
        elem.value = value
        if value:
            if self.mode == "screen":
                self.user.set_screen(value)
            self.activate_unit(self.name2unit.get(value))

    async def select_command(self, _, value):
        _.value = None
        return await self.process_word(value)

    # ------------------------------------------------------------------
    # Word / string processing entry points
    # ------------------------------------------------------------------

    async def process_string(self, string: str) -> Any:
        """Split a sentence into words and process each sequentially."""
        screen_changed = None
        for word in string.split():
            if word:
                result = await self.process_word(word)
                if not screen_changed:
                    screen_changed = result
        return screen_changed

    async def process_word(self, word: str) -> Any:
        """Route a single recognised word to the handler for the current mode."""
        self.input.value = word
        self.message.value = ""
        if not word:
            return None

        command = word2command.get(word)

        match self.mode:
            case "number" | "text":
                return await self._process_input_mode(word, command)
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
                    return await self.run_command(command)
                self.message.value = "Unknown command."
        return None

    # ------------------------------------------------------------------
    # Mode handlers (called from process_word)
    # ------------------------------------------------------------------

    async def _process_input_mode(self, word: str, command: str | None) -> Any:
        """Handle words in text and number modes."""
        if self.mode == "number":
            # FIX: command MUST be checked first. In the original, word_to_number
            # was tried first so "backspace" etc. always fell through to "Not a number".
            if command:
                return await self.run_command(command)
            num = word_to_number(word)
            if num is not None:
                self.previous_unit_value_x = (
                    getattr(self.unit, "value", None),
                    getattr(self.unit, "x", 0),
                )
                self.unit.value = num
            else:
                self.message.value = "Not a number"

        else:  # text
            value = getattr(self.unit, "value", "") or ""
            ux = getattr(self.unit, "x", len(value))

            if self.buffer and self.buffer[-1] == word and command:
                # Double-repeat of the same command word → execute it
                self.buffer.pop()
                if self.previous_unit_value_x:
                    self.unit.value, self.unit.x = self.previous_unit_value_x
                    self.previous_unit_value_x = None
                return await self.run_command(command)

            self.previous_unit_value_x = value, ux
            self.buffer = [word]
            if ux == -1:
                self.unit.value = (value + " " + word) if value else word
                self.unit.x = len(self.unit.value)
            else:
                padded = word + " "
                self.unit.value = value[:ux] + padded + value[ux:]
                self.unit.x = ux + len(padded)
        return None

    async def _process_choice_mode(self, word: str, command: str | None) -> Any:
        """Handle words in switch/check/select/list/radio/tree modes."""
        if command:
            return await self.run_command(command)

        if self.context:
            self.message.value = ""
            self.unit.value = (
                self.context in ("true", "yes", "on")
                if self.mode == "switch"
                else self.context
            )
        else:
            choice, similarity = self._buffer_suits_name(word)
            if similarity >= 0.8:
                self.unit.value = (
                    choice in ("true", "yes", "on")
                    if self.mode == "switch"
                    else choice
                )
                self.message.value = ""
            elif choice:
                self.context = choice
                self.message.value = '"Ok" to confirm'
            else:
                self.commands = []
                self.message.value = "Continue.."
                self.buffer = []
                self.input.value = ""
        return None

    async def _process_root_mode(self, word: str, command: str | None) -> Any:
        """Handle words in root (element selection) mode."""
        if command == "ok" and self.context:
            self.activate_unit(self.name2unit.get(self.context))
        elif command:
            return await self.run_command(command)
        else:
            unit_name, similarity = self._buffer_suits_name(word)
            if similarity >= 0.8:
                self.activate_unit(self.name2unit.get(unit_name))
            elif unit_name:
                self.context = unit_name
                self.message.value = '"Ok" to confirm'
            else:
                self.commands = []
                self.message.value = "Continue.."
                self.input.value = " ".join(self.buffer)
        return None

    async def _process_screen_mode(self, word: str, command: str | None) -> Any:
        """Handle words in screen-navigation mode."""
        if command == "ok" and self.context:
            self.user.set_screen(self.context)
        else:
            screen_name, similarity = self._buffer_suits_name(word)
            if similarity > 0.9:
                self.user.set_screen(screen_name)
            else:
                self.context = screen_name
                self.message.value = '"Ok" to confirm'
        return None

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def run_command(self, command: str) -> Any:
        """Execute a root-level or context command."""
        self.message.value = ""
        match command:
            case "root" | "reset":
                self.reset()
            case "screen":
                self.set_mode("screen")
            case "stop":
                self.stop()
            case _:
                if self.unit:
                    return await self.context_command(command)
                self.message.value = "Command is out of context."
        return None

    async def context_command(self, command: str) -> Any:
        """Execute a command in the context of the currently active unit."""
        u = self.unit
        match self.mode:
            case "text":
                return await self._text_command(u, command)
            case "number":
                return await self._number_command(u, command)
            case "graph" | "net":
                return await self._graph_command(u, command)
            case "table":
                return await self._table_command(u, command)
            case "command":
                if command in ("ok", "push"):
                    handler = getattr(u, "changed", None)
                    if handler:
                        return await call_anysync(handler, u, None)
                self.message.value = "Command is outside context"
        return None

    # ------------------------------------------------------------------
    # Text mode
    # ------------------------------------------------------------------

    async def _text_command(self, u: Unit, command: str) -> Any:
        value = getattr(u, "value", "") or ""
        # FIX: original used `u.x` directly; guard with getattr
        ux = getattr(u, "x", len(value))
        match command:
            case "left":
                if ux > 0:
                    u.x = ux - 1
            case "right":
                # FIX: original had `< len(u.value) - 1` (off-by-one: cursor
                # could never reach the end of the string)
                if ux < len(value):
                    u.x = ux + 1
            case "backspace":
                if ux > 0:
                    u.value = value[: ux - 1] + value[ux:]
                    u.x = ux - 1
            case "delete":
                # FIX: original had `< len(u.value) - 1` (same off-by-one)
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
                # FIX: original had typo "ouside"
                self.message.value = "Command is outside context"
        return None

    # ------------------------------------------------------------------
    # Number mode
    # ------------------------------------------------------------------

    async def _number_command(self, u: Unit, command: str) -> Any:
        svalue = str(u.value) if getattr(u, "value", None) is not None else ""
        ux = getattr(u, "x", len(svalue))
        match command:
            case "left":
                if ux > 0:
                    u.x = ux - 1
            case "right":
                # FIX: original had `< len(svalue) - 1` (off-by-one)
                if ux < len(svalue):
                    u.x = ux + 1
            case "backspace":
                if ux > 0:
                    raw = svalue[: ux - 1] + svalue[ux:]
                    try:
                        # FIX: original did float(...) without try/except;
                        # this crashes when the remaining string is "" or "-"
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
    # Graph / Net mode
    # ------------------------------------------------------------------

    def _refresh_graph_context(self) -> None:
        """Populate context list with node and edge labels."""
        if self.unit is None:
            self.context_options = []
            self.message.value = "No graph selected"
            return
        self.context_options = self._graph_element_labels()
        self.message.value = (
            "Say: node <name> / edge <name> / add / remove / connect / disconnect / clear"
        )

    def _graph_element_labels(self) -> list[str]:
        """Collect human-readable labels for all nodes and edges."""
        if self.unit is None:
            return []
        labels: list[str] = []
        for i, node in enumerate(getattr(self.unit, "nodes", [])):
            if node is not None:
                label = (
                    getattr(node, "label", None)
                    or getattr(node, "name", None)
                    or str(i)
                )
                labels.append(f"node:{label}")
        for i, edge in enumerate(getattr(self.unit, "edges", [])):
            if edge is not None:
                src = getattr(edge, "source", "?")
                tgt = getattr(edge, "target", "?")
                label = getattr(edge, "label", None) or f"{src}-{tgt}"
                labels.append(f"edge:{label}")
        return sorted(labels)

    async def _process_graph_word(self, word: str, command: str | None) -> Any:
        """Interpret a word in graph/net mode."""
        u = self.unit
        if u is None:
            self.message.value = "No graph active"
            return None

        if command:
            return await self.run_command(command)

        # Pending two-step operations
        if self._graph_pending_action == "add_node":
            return self._graph_add_node(u, word)
        if self._graph_pending_action == "add_edge_target":
            return self._graph_connect(u, word)

        # "node <name>" / "edge <name>" two-step selection
        if self._graph_pending_action and self._graph_pending_action.startswith("select_"):
            kind = self._graph_pending_action.split("_")[1]
            self._graph_select_element(u, kind, word)
            self._graph_pending_action = None
            return None

        if word in ("node", "edge"):
            self.message.value = f"Say {word} name"
            self._graph_pending_action = f"select_{word}"
            return None

        # Fuzzy match
        choice, sim = self._buffer_suits_name(word)
        if sim >= 0.8 and choice:
            self._apply_graph_selection(u, choice)
        elif choice:
            self.context = choice
            self.message.value = '"Ok" to confirm'
        else:
            self.message.value = "Element not found"
        return None

    async def _graph_command(self, u: Unit, command: str) -> Any:
        """Execute a graph manipulation command."""
        match command:
            case "add":
                self._graph_pending_action = "add_node"
                self.message.value = "Say new node name"
            case "remove":
                self._graph_remove_selected(u)
            case "connect":
                self._graph_pending_action = "add_edge_target"
                self._graph_edge_source = self._selected_node_id(u)
                self.message.value = "Say target node name"
            case "disconnect":
                self._graph_remove_selected_edges(u)
            case "select":
                self._refresh_graph_context()
            case "deselect":
                u.value = {"nodes": [], "edges": []}
                self.message.value = "Selection cleared"
            case "clear":
                self.context = "__clear_graph__"
                self.message.value = "Say 'ok' to confirm clear"
            case "ok" if self.context == "__clear_graph__":
                self._graph_clear_all(u)
                self.context = None
            case _:
                self.message.value = "Unknown graph command"
        return None

    def _graph_add_node(self, u: Unit, name: str) -> None:
        nodes = getattr(u, "nodes", None)
        if nodes is None:
            self.message.value = "Graph has no nodes list"
            self._graph_pending_action = None
            return
        from .units import Node
        nodes.append(Node(name))
        self._graph_pending_action = None
        self.context_options = self._graph_element_labels()
        self.message.value = f"Node '{name}' added"

    def _graph_connect(self, u: Unit, target_word: str) -> None:
        if self._graph_edge_source is None:
            self.message.value = "No source node selected"
            self._graph_pending_action = None
            return
        nodes = getattr(u, "nodes", [])
        target_id = self._find_node_id_by_name(nodes, target_word)
        if target_id is None:
            self.message.value = f"Node '{target_word}' not found"
            return
        edges = getattr(u, "edges", None)
        if edges is None:
            self.message.value = "Graph has no edges list"
            self._graph_pending_action = None
            return
        from .units import Edge
        edges.append(Edge(self._graph_edge_source, target_id))
        self._graph_pending_action = None
        self._graph_edge_source = None
        self.context_options = self._graph_element_labels()
        self.message.value = "Edge added"

    def _graph_remove_selected(self, u: Unit) -> None:
        val = getattr(u, "value", {}) or {}
        nodes = getattr(u, "nodes", [])
        edges = getattr(u, "edges", [])
        for nid in sorted(val.get("nodes", []), reverse=True):
            if 0 <= nid < len(nodes):
                nodes[nid] = None  # UNISI convention: null-mark removed items
        for eid in sorted(val.get("edges", []), reverse=True):
            if 0 <= eid < len(edges):
                edges[eid] = None
        u.value = {"nodes": [], "edges": []}
        self.context_options = self._graph_element_labels()
        self.message.value = "Selected elements removed"

    def _graph_remove_selected_edges(self, u: Unit) -> None:
        val = getattr(u, "value", {}) or {}
        edges = getattr(u, "edges", [])
        for eid in sorted(val.get("edges", []), reverse=True):
            if 0 <= eid < len(edges):
                edges[eid] = None
        u.value = {"nodes": val.get("nodes", []), "edges": []}
        self.context_options = self._graph_element_labels()
        self.message.value = "Selected edges removed"

    def _graph_clear_all(self, u: Unit) -> None:
        if hasattr(u, "nodes"):
            u.nodes = []
        if hasattr(u, "edges"):
            u.edges = []
        u.value = {"nodes": [], "edges": []}
        self.context_options = []
        self.message.value = "Graph cleared"

    def _graph_select_element(self, u: Unit, kind: str, name: str) -> None:
        val = getattr(u, "value", {"nodes": [], "edges": []}) or {"nodes": [], "edges": []}
        if kind == "node":
            idx = self._find_node_id_by_name(getattr(u, "nodes", []), name)
            if idx is not None:
                val["nodes"] = [idx]
                u.value = val
                self.message.value = f"Node '{name}' selected"
            else:
                self.message.value = f"Node '{name}' not found"
        else:
            idx = self._find_edge_id_by_label(getattr(u, "edges", []), name)
            if idx is not None:
                val["edges"] = [idx]
                u.value = val
                self.message.value = f"Edge '{name}' selected"
            else:
                self.message.value = f"Edge '{name}' not found"

    def _apply_graph_selection(self, u: Unit, label: str) -> None:
        """Apply selection from a fuzzy-matched 'node:Label' / 'edge:Label' string."""
        if ":" in label:
            kind, name = label.split(":", 1)
            self._graph_select_element(u, kind, name)
        else:
            self.message.value = "Ambiguous element"

    def _selected_node_id(self, u: Unit) -> int | None:
        val = getattr(u, "value", {}) or {}
        nodes = val.get("nodes", [])
        return nodes[0] if nodes else None

    @staticmethod
    def _find_node_id_by_name(nodes: list, name: str) -> int | None:
        name_lower = name.lower()
        for i, node in enumerate(nodes):
            if node is None:
                continue
            candidate = (
                getattr(node, "label", None)
                or getattr(node, "name", None)
                or str(i)
            )
            if candidate.lower() == name_lower:
                return i
        return None

    @staticmethod
    def _find_edge_id_by_label(edges: list, name: str) -> int | None:
        name_lower = name.lower()
        for i, edge in enumerate(edges):
            if edge is None:
                continue
            label = (
                getattr(edge, "label", None)
                or f"{getattr(edge, 'source', '?')}-{getattr(edge, 'target', '?')}"
            )
            if label.lower() == name_lower:
                return i
        return None

    # ------------------------------------------------------------------
    # Table mode
    # ------------------------------------------------------------------

    async def _process_table_word(self, word: str, command: str | None) -> Any:
        """Interpret a word in table mode (fuzzy column header matching)."""
        if self.unit is None:
            self.message.value = "No table active"
            return None
        if command:
            return await self.run_command(command)
        headers = getattr(self.unit, "headers", [])
        if headers:
            choice, sim = self._buffer_suits_name(word)
            if sim >= 0.8 and choice in headers:
                self._table_col = headers.index(choice)
                self._announce_table_position()
                return None
        self.message.value = "Unknown table command"
        return None

    async def _table_command(self, u: Unit, command: str) -> Any:
        """Execute a table navigation or editing command."""
        rows = getattr(u, "rows", [])
        headers = getattr(u, "headers", [])
        total_rows = len(rows)
        total_cols = len(headers)

        match command:
            case "next" | "down":
                # Advance the internal cursor AND update u.value to reflect selection
                if self._table_row < total_rows - 1:
                    self._table_row += 1
                # Propagate to table widget: mirror variant B's value[0] idiom
                val = getattr(u, "value", None)
                if isinstance(val, list):
                    u.value = [self._table_row]
                self._announce_table_position()

            case "prev" | "up":
                if self._table_row > 0:
                    self._table_row -= 1
                val = getattr(u, "value", None)
                if isinstance(val, list):
                    u.value = [self._table_row]
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
                self._table_row = min(self._table_row + 10, total_rows - 1)
                val = getattr(u, "value", None)
                if isinstance(val, list):
                    u.value = [self._table_row]
                self._announce_table_position()

            case "row":
                # Explicit row selection; delegate to changed handler if defined
                u.value = self._table_row
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
                    self.message.value = f"Cell value: {cell_val!r} — dictate new value"
                else:
                    self.message.value = "No cell selected"

            case "confirm" | "enter":
                update_handler = getattr(u, "update", None)
                if update_handler:
                    return await call_anysync(
                        update_handler, u, (self._table_row, self._table_col)
                    )
                self.message.value = "No update handler configured"

            case _:
                # Forward unknown commands to the table's own changed handler
                # (mirrors variant B's fallback approach for page/column/etc.)
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
    # Internal helpers
    # ------------------------------------------------------------------

    def _buffer_suits_name(self, word: str) -> tuple[str, float]:
        """Append word to the buffer and fuzzy-match against context options."""
        self.buffer.append(word)
        name = " ".join(self.buffer)
        return find_most_similar_sequence(name, self.context_options)