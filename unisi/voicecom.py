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
    In text mode, root commands are recognised FIRST and never
    inserted into the text field.

Escaping text / number mode
----------------------------
The only intentional ways to leave an input mode are:
  • Say "root" or "reset" → go back to element-selection mode
  • Say "screen"          → go to screen-navigation mode
  • Say "stop"            → hide the Mate block
  • Complete the edit and say "ok" / "enter"

Two-word escape (kept from original UX design):
  If the last word in the buffer equals the new word AND that word maps
  to a command, execute the command instead of inserting it.
  Example: user says "delete" to type the word, says "delete" again →
  execute the delete-character command.
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
        # w2n raises various exception types across versions
        return None


# ---------------------------------------------------------------------------
# Command vocabulary
# ---------------------------------------------------------------------------

command_synonyms: dict[str, list[str]] = dict(
    value=["is", "equals"],
    root=["select", "choose", "set"],       # "select" → "root" → reset to elem selection
    backspace=["back"],
    enter=["push", "execute", "run"],
    clean=["empty", "erase"],
    screen=["menu"],
    push=["execute", "run"],
    reset=["cancel"],
    ok=["okay"],
)

# Commands that navigate at session level – always honoured, in every mode.
# FIX: these must escape text/number mode without being inserted into the field.
root_commands: list[str] = ["root", "screen", "stop", "reset", "ok"]
ext_root_commands: list[str] = root_commands[:]   # extended with synonyms below

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

# flat reverse-lookup: spoken word → canonical command token
word2command: dict[str, str] = {}
for _cmd, _syns in command_synonyms.items():
    for _syn in _syns:
        word2command[_syn] = _cmd
    if _cmd in root_commands:
        ext_root_commands.extend(_syns)

word2command.update({c: c for c in root_commands})
for _mode_cmds in modes.values():
    word2command.update({c: c for c in _mode_cmds})

# Set of command tokens that are global escape commands
_ROOT_CMD_SET: frozenset[str] = frozenset(["root", "reset", "screen", "stop"])


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
        self.cached_commands: dict[str, list[str]] = {}
        # Table cursor state
        self._table_row: int = 0
        self._table_col: int = 0
        # Graph two-step pending state
        self._graph_pending_action: str | None = None
        self._graph_edge_source: int | None = None

        self.block = self._build_assist_block(user)
        self.set_screen(user.screen)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_assist_block(self, user) -> Block:
        """Create the floating Mate helper block."""
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
        """Switch to a new screen and rebuild the interactive-element index.

        FIX: original called calc_interactive_units() BEFORE assigning
        self.screen, so it always scanned the *previous* screen. Order is
        now: assign first, index second.
        """
        self.screen = screen
        self.calc_interactive_units()
        self.reset()

    def calc_interactive_units(self) -> None:
        """Index all editable units on the current screen by their pretty name.

        Reads self.screen (set by set_screen before this call), NOT
        self.user.screen. The two may differ: UNISI calls VoiceCom.set_screen()
        passing the new screen object as an argument, but self.user.screen may
        still point to the old screen at that moment if the user object updates
        its own reference later. Always use self.screen to guarantee we index
        the screen we were actually asked to switch to.
        """
        interactive_names: list[str] = []
        name2unit: dict[str, Unit] = {}
        self.screen_name = self.screen.name
        for block in flatten(self.screen.blocks):
            for elem in flatten(block.value):
                if getattr(elem, "edit", True):
                    pretty_name = pretty4(elem.name)
                    name2unit[pretty_name] = elem
                    interactive_names.append(pretty_name)
        interactive_names.sort()
        self.unit_names = interactive_names
        self.name2unit = name2unit

    def activate_unit(self, unit: Unit | None) -> None:
        """Deactivate the previous unit, activate the new one, enter its mode.

        FIX: if `unit` is None (name not found in name2unit), show an error
        message instead of silently doing nothing. Previously the caller passed
        name2unit.get(key) which returns None for a missing key, leaving the
        voice controller in an inconsistent state with self.unit unchanged.
        """
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

        match unit.type:
            case "string":
                mode = "text"
            case "range":
                mode = "number"
            case _:
                mode = unit.type

        self.set_mode(mode)

        if mode in ("text", "number"):
            self.previous_unit_value_x = (
                getattr(unit, "value", None),
                getattr(unit, "x", 0),
            )

    def set_mode(self, mode: str) -> None:
        """Configure commands and context list for an interaction mode."""
        self.context = None
        self.mode = mode
        self.buffer = []
        self.previous_unit_value_x = None
        self._graph_pending_action = None
        self._graph_edge_source = None

        if mode not in self.cached_commands:
            # FIX: copy list – never mutate the module-level modes[] dict
            cmds = list(modes.get(mode, [])) + root_commands
            extra: list[str] = []
            for cmd in cmds:
                if cmd in command_synonyms:
                    extra.extend(command_synonyms[cmd])
            cmds.extend(extra)
            cmds = sorted(set(cmds))
            self.cached_commands[mode] = cmds

        self.commands = self.cached_commands[mode]
        self.input.value = mode
        self.message.value = "Continue.."

        match mode:
            case "switch" | "check":
                self.context_options = ["true", "false", "yes", "no", "on", "off"]
            case "select" | "list" | "radio" | "tree":
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
            case "text":
                # FIX: context_options in text mode shows all OTHER element names
                # so the user can see what they can switch to without leaving.
                # The context list is NOT used for fuzzy matching of typed words.
                self.context_options = self.unit_names
                self.message.value = "Dictate text. Say 'root' or 'reset' to switch element."
            case _:
                self.context_list.options = []

        self.context = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Show the Mate block on the current screen.

        FIX: screen.blocks is stored as a tuple by the UNISI Unit proxy.
        Convert to list, mutate, write back.
        """
        blocks = list(self.screen.blocks)
        if self.block not in blocks:
            blocks.append(self.block)
            self.screen.blocks = blocks
        self.reset()

    def stop(self) -> None:
        """Hide the Mate block."""
        blocks = list(self.screen.blocks)
        if self.block in blocks:
            blocks.remove(self.block)
            self.screen.blocks = blocks

    def reset(self) -> None:
        """Return to root mode; clear all transient state."""
        self.buffer = []
        self.mode = "root"
        self._table_row = 0
        self._table_col = 0
        self._graph_pending_action = None
        self._graph_edge_source = None

        if dialog := getattr(self.user, "active_dialog", None):
            cmds = sorted(set(ext_root_commands + ["close"]))
            self.commands = cmds
            options = sorted(u.name for u in flatten(dialog.value))
            self.context_options = options
        else:
            self.commands = sorted(set(ext_root_commands))
            self.context_options = getattr(self, "unit_names", [])

        if self.unit:
            self.unit.active = False
            self.unit.focus = False
            self.unit = None

        self.input.value = ""
        self.message.value = "Select an element or command"
        self.context = None

    # ------------------------------------------------------------------
    # Event handlers wired to widgets
    # ------------------------------------------------------------------

    async def keyboard_input(self, _, value: str):
        return await self.process_string(value)

    def select_elem(self, elem, value: str) -> None:
        """Called when the user taps an entry in the context_list widget.

        FIX: the original always called activate_unit() even in screen mode,
        causing a double action (set_screen AND activate_unit). Now the two
        paths are mutually exclusive.
        """
        elem.value = value
        if not value:
            return
        if self.mode == "screen":
            self.user.set_screen(value)
            # set_screen triggers set_screen → reset internally; don't activate
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
        """Split into words and process each sequentially."""
        screen_changed = None
        for word in string.split():
            if word:
                result = await self.process_word(word)
                if not screen_changed:
                    screen_changed = result
        return screen_changed

    async def process_word(self, word: str) -> Any:
        """Route a single spoken word to the handler for the current mode."""
        self.input.value = word
        self.message.value = ""
        if not word:
            return None

        command = word2command.get(word)

        # FIX: root-level navigation commands (root/reset/screen/stop) must
        # ALWAYS be honoured regardless of mode. They are the only escape hatch
        # from text/number/graph/table modes. We intercept them here before
        # dispatching to mode-specific handlers. "ok" is intentionally excluded
        # because it has mode-specific meaning (confirm a pending choice).
        if command in _ROOT_CMD_SET:
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
        """Execute a session-level navigation command.

        FIX: previously run_command() routed root/reset/screen/stop to
        context_command() when self.unit was set, where they were unhandled.
        These commands now bypass context_command entirely.
        """
        self.buffer = []            # always clear the input buffer on escape
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
        """Handle a word while a text field is active.

        Design contract
        ---------------
        • Root commands are intercepted upstream – they never reach here.
        • Any other command word is inserted literally on the FIRST occurrence.
        • If the same word is spoken again immediately (double-tap pattern) AND
          it maps to a text-editing command, execute that command instead.
        • This lets users say "delete" (inserts "delete "), then "delete" again
          to actually delete the preceding character.
        """
        value = getattr(self.unit, "value", "") or ""
        ux = getattr(self.unit, "x", len(value))

        # Double-tap: same word as last buffer entry AND it's a text command
        text_commands = set(modes["text"])
        if (command and command in text_commands
                and self.buffer and self.buffer[-1] == word):
            self.buffer = []        # FIX: clear buffer after executing command
            if self.previous_unit_value_x:
                self.unit.value, self.unit.x = self.previous_unit_value_x
                self.previous_unit_value_x = None
            return await self._text_command(self.unit, command)

        # Insert word into the field
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

    # ------------------------------------------------------------------
    # Number mode
    # ------------------------------------------------------------------

    async def _process_number_mode(self, word: str, command: str | None) -> Any:
        """Handle a word while a number field is active.

        FIX: command MUST be checked FIRST. In the original, word_to_number()
        was tried first, so "backspace"/"undo" always fell through to
        "Not a number" because they convert to None.
        """
        if command:
            self.buffer = []        # clear after any command
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

    # ------------------------------------------------------------------
    # Choice mode  (switch / check / select / list / radio / tree)
    # ------------------------------------------------------------------

    async def _process_choice_mode(self, word: str, command: str | None) -> Any:
        """Handle a word in a selection-style mode.

        FIX: "ok" now correctly confirms a pending context choice here
        rather than going to context_command where it was unhandled.
        """
        # "ok" confirms a pending fuzzy match
        if command == "ok":
            if self.context:
                self.message.value = ""
                self.buffer = []
                self._apply_choice(self.context)
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
            self.context = choice
            self.message.value = '"Ok" to confirm'
        else:
            self.commands = self.cached_commands.get(self.mode, [])
            self.message.value = "Continue.."
            self.buffer = []
            self.input.value = ""
        return None

    def _apply_choice(self, choice: str) -> None:
        """Write a confirmed selection value to the active unit."""
        if self.mode == "switch":
            self.unit.value = choice in ("true", "yes", "on")
        else:
            self.unit.value = choice

    # ------------------------------------------------------------------
    # Root mode  (element selection)
    # ------------------------------------------------------------------

    async def _process_root_mode(self, word: str, command: str | None) -> Any:
        """Handle a word in root (element-selection) mode."""
        if command == "ok":
            if self.context:
                unit = self.name2unit.get(self.context)
                self.context = None
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
            self.context = unit_name
            self.message.value = '"Ok" to confirm'
        else:
            self.commands = sorted(set(ext_root_commands))
            self.message.value = "Continue.."
            self.input.value = " ".join(self.buffer)
        return None

    # ------------------------------------------------------------------
    # Screen mode
    # ------------------------------------------------------------------

    async def _process_screen_mode(self, word: str, command: str | None) -> Any:
        if command == "ok":
            if self.context:
                self.user.set_screen(self.context)
            else:
                self.message.value = "Nothing to confirm"
            return None

        screen_name, similarity = self._buffer_suits_name(word)
        if similarity > 0.9:
            self.buffer = []
            self.user.set_screen(screen_name)
        elif screen_name:
            self.context = screen_name
            self.message.value = '"Ok" to confirm'
        return None

    # ------------------------------------------------------------------
    # Shared command dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_context_command(self, command: str) -> Any:
        """Route a non-root command to the correct mode handler."""
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
                # ok is handled upstream; remaining commands fall here
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
    # Text editing commands
    # ------------------------------------------------------------------

    async def _text_command(self, u: Unit, command: str) -> Any:
        value = getattr(u, "value", "") or ""
        ux = getattr(u, "x", len(value))
        match command:
            case "left":
                if ux > 0:
                    u.x = ux - 1
            case "right":
                # FIX: original had < len - 1 (off-by-one, cursor couldn't reach end)
                if ux < len(value):
                    u.x = ux + 1
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
    # Number editing commands
    # ------------------------------------------------------------------

    async def _number_command(self, u: Unit, command: str) -> Any:
        svalue = str(getattr(u, "value", "")) if getattr(u, "value", None) is not None else ""
        ux = getattr(u, "x", len(svalue))
        match command:
            case "left":
                if ux > 0:
                    u.x = ux - 1
            case "right":
                if ux < len(svalue):
                    u.x = ux + 1
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
    # Graph / Net mode
    # ------------------------------------------------------------------

    def _refresh_graph_context(self) -> None:
        if self.unit is None:
            self.context_options = []
            self.message.value = "No graph selected"
            return
        self.context_options = self._graph_element_labels()
        self.message.value = (
            "Say: add / remove / connect / disconnect / clear  "
            "or speak a node/edge name to select it"
        )

    def _graph_element_labels(self) -> list[str]:
        labels: list[str] = []
        for i, node in enumerate(getattr(self.unit, "nodes", [])):
            if node is not None:
                label = (getattr(node, "label", None)
                         or getattr(node, "name", None)
                         or str(i))
                labels.append(f"node:{label}")
        for i, edge in enumerate(getattr(self.unit, "edges", [])):
            if edge is not None:
                src = getattr(edge, "source", "?")
                tgt = getattr(edge, "target", "?")
                label = getattr(edge, "label", None) or f"{src}-{tgt}"
                labels.append(f"edge:{label}")
        return sorted(labels)

    async def _process_graph_word(self, word: str, command: str | None) -> Any:
        u = self.unit
        if u is None:
            self.message.value = "No graph active"
            return None

        if command:
            return await self._dispatch_context_command(command)

        if self._graph_pending_action == "add_node":
            return self._graph_add_node(u, word)
        if self._graph_pending_action == "add_edge_target":
            return self._graph_connect(u, word)
        if self._graph_pending_action and self._graph_pending_action.startswith("select_"):
            kind = self._graph_pending_action.split("_", 1)[1]
            self._graph_select_element(u, kind, word)
            self._graph_pending_action = None
            return None
        if word in ("node", "edge"):
            self._graph_pending_action = f"select_{word}"
            self.message.value = f"Say {word} name"
            return None

        choice, sim = self._buffer_suits_name(word)
        if sim >= 0.8 and choice:
            self.buffer = []
            self._apply_graph_selection(u, choice)
        elif choice:
            self.context = choice
            self.message.value = '"Ok" to confirm'
        else:
            self.message.value = "Element not found"
        return None

    async def _graph_command(self, u: Unit, command: str) -> Any:
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
                self.message.value = "Say 'ok' to confirm clearing the graph"
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
                nodes[nid] = None
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
            candidate = (getattr(node, "label", None)
                         or getattr(node, "name", None)
                         or str(i))
            if candidate.lower() == name_lower:
                return i
        return None

    @staticmethod
    def _find_edge_id_by_label(edges: list, name: str) -> int | None:
        name_lower = name.lower()
        for i, edge in enumerate(edges):
            if edge is None:
                continue
            label = (getattr(edge, "label", None)
                     or f"{getattr(edge,'source','?')}-{getattr(edge,'target','?')}")
            if label.lower() == name_lower:
                return i
        return None

    # ------------------------------------------------------------------
    # Table mode
    # ------------------------------------------------------------------

    async def _process_table_word(self, word: str, command: str | None) -> Any:
        if self.unit is None:
            self.message.value = "No table active"
            return None
        if command:
            return await self._dispatch_context_command(command)
        headers = getattr(self.unit, "headers", [])
        if headers:
            choice, sim = self._buffer_suits_name(word)
            if sim >= 0.8 and choice in headers:
                self.buffer = []
                self._table_col = headers.index(choice)
                self._announce_table_position()
                return None
        self.message.value = "Unknown table command"
        return None

    async def _table_command(self, u: Unit, command: str) -> Any:
        rows = getattr(u, "rows", [])
        headers = getattr(u, "headers", [])
        total_rows = len(rows)
        total_cols = len(headers)

        match command:
            case "next" | "down":
                if self._table_row < total_rows - 1:
                    self._table_row += 1
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
                self._table_row = min(self._table_row + 10, max(total_rows - 1, 0))
                val = getattr(u, "value", None)
                if isinstance(val, list):
                    u.value = [self._table_row]
                self._announce_table_position()
            case "row":
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
        return find_most_similar_sequence(name, self.context_options)
