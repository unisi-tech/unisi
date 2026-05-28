# Copyright © 2024 UNISI Tech. All rights reserved.
from websocket import create_connection
from enum import IntFlag
import json, requests, os

from urllib.parse import quote
from .common import *
class Event(IntFlag):
    none = 0
    update = 1
    invalid = 2
    message = 4
    update_message = 5
    progress = 8
    update_progress = 9
    unknown = 16
    unknown_update = 17
    dialog = 32
    screen = 65
    complete = 128
    append = 256
ws_header = 'ws://'
wss_header = 'wss://'
ws_path = 'ws'

message_types = ['error', 'warning', 'info']
class Proxy:
    """UNISI proxy"""

    def __init__(self, host_port, timeout=7, ssl=False, session='', screen=None):
        """
        Connect to a UNISI server.

        host_port : str  — e.g. 'localhost:8000'
        timeout   : int  — WebSocket timeout in seconds
        ssl       : bool — use wss:// / https://
        session   : str  — optional session query-string token
        screen    : str  — optional screen name to activate immediately on connect.
                           Mirrors the server-side User.__init__(screen=) parameter.
        """
        addr_port = f'{wss_header if ssl else ws_header}{host_port}'
        addr_port = f'{addr_port}{"" if addr_port.endswith("/") else "/"}{ws_path}'
        self.host_port = f'{"https" if ssl else "http"}://{host_port}'

        # Build query string: session token and/or initial screen name
        params = []
        if session:
            params.append(session)
        if screen:
            params.append(f'screen={quote(screen, safe="")}')
        if params:
            addr_port = f'{addr_port}?{"&".join(params)}'

        self.conn = create_connection(addr_port, timeout=timeout)
        self.screen = None
        self.screens = {}
        self.dialog = None
        self.event = None
        self.request(None)

    # ──────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────

    def close(self):
        self.conn.close()

    # ──────────────────────────────────────────────
    # Screen navigation
    # ──────────────────────────────────────────────

    @property
    def screen_menu(self):
        return [name_icon[0] for name_icon in self.screen['menu']] if self.screen else []

    def set_screen(self, name):
        """
        Switch the active screen.

        Always sends a request to the server so the server session stays in
        sync — even when the screen was visited before and is cached locally.
        Returns True on success.
        """
        if name not in self.screen_menu:
            return False
        mtype = self.request(ArgObject(block='root', element=None, value=name))
        return mtype == Event.screen

    # ──────────────────────────────────────────────
    # Internal block/element traversal helpers
    # ──────────────────────────────────────────────

    @staticmethod
    def _iter_block_elements(value):
        """
        Recursively yield every non-block element inside *value*.

        The server stores nested blocks as dicts with type=='block' inside the
        'value' list of a parent block.  A plain flatten() only goes one level
        deep; this generator descends into every nested block automatically.
        """
        for item in flatten(value):
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'block':
                yield from Proxy._iter_block_elements(item.get('value', []))
            else:
                yield item

    def _root_blocks(self):
        """
        Yield the top-level blocks of the current screen (not nested ones).

        Iteration for element search must start only from root blocks and
        recurse downward.  Iterating name2block directly would visit nested
        blocks both via their parent (recursion) and directly (indexed entry),
        causing every nested element to appear twice and be flagged as ambiguous.
        """
        if not self.screen:
            return
        for block in flatten(self.screen.get('blocks', [])):
            if isinstance(block, dict):
                yield block
        # toolbar is also a searchable scope
        toolbar_value = self.screen.get('toolbar', [])
        if toolbar_value:
            yield {'name': 'toolbar', 'value': toolbar_value}

    def _build_name2block(self, blocks):
        """
        Build a flat name→block mapping by walking the block tree recursively.

        Every block at every nesting depth is indexed so that direct lookups
        (e.g. update() by path[0] block name) work regardless of depth.
        """
        result = {}
        for block in flatten(blocks):
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'block' or block.get('name'):
                result[block['name']] = block
            for item in flatten(block.get('value', [])):
                if isinstance(item, dict) and item.get('type') == 'block':
                    result.update(self._build_name2block([item]))
        return result

    def _find_parent_block(self, block_dict):
        """
        Find the direct parent block of *block_dict* among all indexed blocks.

        Returns the parent dict or None if *block_dict* is a root block.
        """
        target_name = block_dict.get('name')
        for candidate in self.screen['name2block'].values():
            if candidate is block_dict:
                continue
            for item in flatten(candidate.get('value', [])):
                if isinstance(item, dict) and item.get('name') == target_name \
                        and item.get('type') == 'block':
                    return candidate
        return None

    def _block_path(self, block_dict):
        """
        Return the '@'-separated path string the server expects in the 'block'
        field of a message: 'inner@outer' (deepest first, matching strpath /
        find_element logic in users.py).

        Top-level blocks return just their own name.
        """
        parent = self._find_parent_block(block_dict)
        if parent is None:
            return block_dict['name']
        return f'{block_dict["name"]}@{self._block_path(parent)}'

    def _owning_block(self, elem_name):
        """
        Return the *direct* containing block dict for the element named
        *elem_name*, searching the full nested tree.

        Starts from root blocks so each element is found exactly once.
        """
        def _search(block_dict):
            for item in flatten(block_dict.get('value', [])):
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'block':
                    found = _search(item)
                    if found is not None:
                        return found
                elif item.get('name') == elem_name:
                    return block_dict
            return None

        for root_block in self._root_blocks():
            found = _search(root_block)
            if found is not None:
                return found
        return None

    # ──────────────────────────────────────────────
    # Public element API
    # ──────────────────────────────────────────────

    @property
    def commands(self):
        """Return all command (button) elements on the current screen."""
        return self.elements(types=['command'])

    def element(self, name, block_name=None):
        """
        Return the element with *name*, or None if not found or ambiguous.

        block_name : str  — restrict the search to a specific block (optional).
                           The block can be at any nesting depth; it will be
                           looked up via name2block.
        """
        result = None

        if block_name:
            # Search only within the specified block and its nested children
            blk = self.screen['name2block'].get(block_name)
            if blk is None:
                return None
            for el in self._iter_block_elements(blk.get('value', [])):
                if el.get('name') == name:
                    if result is None:
                        result = el
                    else:
                        return None  # ambiguous
            return result

        # No block filter — search all root blocks recursively.
        # Using _root_blocks() ensures each element is visited exactly once
        # even when nested blocks are also stored in name2block.
        for root_block in self._root_blocks():
            for el in self._iter_block_elements(root_block.get('value', [])):
                if el.get('name') == name:
                    if result is None:
                        result = el
                    else:
                        return None  # ambiguous
        return result

    def elements(self, block=None, types=None):
        """
        Return elements on the current screen, optionally filtered.

        block : dict  — restrict to this block dict and its nested children.
        types : list  — restrict to these element type strings.
        """
        if block:
            return [
                el for el in self._iter_block_elements(block.get('value', []))
                if not types or el.get('type') in types
            ]

        answer = []
        # Iterate from root blocks only to avoid double-counting nested elements
        for root_block in self._root_blocks():
            answer.extend(
                el for el in self._iter_block_elements(root_block.get('value', []))
                if not types or el.get('type') in types
            )
        return answer

    def block_name(self, element):
        """
        Return the '@'-separated block path for *element* (str name or dict).

        The path format matches what the server expects: 'inner@outer'.
        Nested elements return a multi-segment path; top-level elements return
        just their block's name.  Returns None if the element is not found.
        """
        elem_name = element if isinstance(element, str) else element.get('name')
        owning = self._owning_block(elem_name)
        if owning is None:
            return None
        return self._block_path(owning)

    # ──────────────────────────────────────────────
    # File upload
    # ──────────────────────────────────────────────

    def upload(self, fpath):
        """Upload a file to the server and return its server-side path."""
        with open(fpath, 'rb') as file:
            response = requests.post(
                self.host_port, files={os.path.basename(fpath): file}
            )
        return getattr(response, 'text', '')

    # ──────────────────────────────────────────────
    # Commands
    # ──────────────────────────────────────────────

    def command(self, command, value=None):
        return self.interact(self.make_message(command, value))

    def command_upload(self, command, fpath):
        """Upload *fpath* to the server and trigger *command*."""
        spath = os.path.abspath(fpath) if 'localhost' in self.host_port else self.upload(fpath)
        return self.command(command, spath) if spath else Event.invalid

    def make_message(self, element, value=None, event='changed'):
        if isinstance(element, str):
            element = self.element(element)
        if element is None:
            return None
        if event != 'changed' and event not in element:
            return None
        return ArgObject(
            block=self.block_name(element),
            element=element['name'],
            event=event,
            value=value,
        )

    # ──────────────────────────────────────────────
    # Value setter
    # ──────────────────────────────────────────────

    def set_value(self, element, new_value):
        if isinstance(element, str):
            element = self.element(element)
        if element is None:
            return Event.invalid
        element['value'] = new_value
        ms = self.make_message(element, new_value)
        return self.interact(ms) if ms else Event.invalid

    # ──────────────────────────────────────────────
    # Transport
    # ──────────────────────────────────────────────

    def interact(self, message, progress_callback=None):
        """
        Send *message*, consuming server responses until no longer in a
        progress state.

        progress_callback : callable(proxy) — called on each progress tick.
        """
        while self.request(message) & Event.progress:
            if progress_callback:
                progress_callback(self)
            message = None
        return self.event

    def request(self, message):
        """Send *message* (or None to only receive), parse and process response."""
        if message:
            self.conn.send(toJson(message))
        raw = self.conn.recv()
        data = json.loads(raw)
        return self.process(data)

    # ──────────────────────────────────────────────
    # Dialog
    # ──────────────────────────────────────────────

    @property
    def dialog_commands(self):
        return self.dialog['commands'] if self.dialog else []

    def dialog_responce(self, command: str | None):
        if not self.dialog:
            self.event = Event.invalid
            return self.event
        return self.interact(ArgObject(block=self.dialog['name'], value=command))

    # ──────────────────────────────────────────────
    # Message processing
    # ──────────────────────────────────────────────

    def process(self, message):
        self.message = message
        if not message:
            self.event = Event.none
            self.mtype = None
            return self.event

        mtype = message.get('type')
        self.mtype = mtype

        if mtype == 'screen':
            self.screen = message
            self.screens[message['name']] = message
            # Build a recursive name→block index for direct lookups by name
            name2block = self._build_name2block(message.get('blocks', []))
            name2block['toolbar'] = {'name': 'toolbar', 'value': message.get('toolbar', [])}
            message['name2block'] = name2block
            self.event = Event.screen

        elif mtype == 'dialog':
            self.dialog = message
            self.event = Event.dialog

        elif mtype == 'complete':
            return Event.complete

        elif mtype == 'append':
            self.event = Event.append

        elif mtype == 'update':
            self.update(message)
            self.event = Event.update

        else:
            updates = message.get('updates')
            if updates:
                self.update(message)

            # FIX: original used bare `type` (Python built-in function),
            # which is never equal to a string. Replaced with `mtype`.
            if mtype in message_types:
                self.event = Event.update_message if updates else Event.message
            elif mtype == 'progress':
                self.event = Event.update_progress if updates else Event.progress
            else:
                self.event = Event.unknown_update if updates else Event.unknown

        return self.event

    def update(self, message):
        """
        Apply incremental updates from *message* to the local screen state.

        Fixes applied vs original:
          1. message.updates  → message.get('updates', [])
             (message is a plain dict decoded from JSON, not an ArgObject)
          2. name2block[block] with undefined variable `block` → path[0]
             (the block name is the first element of the path list)
          3. el.__dict__ = update['data'].__dict__ → el.update(data)
             (data is a dict; objects don't have __dict__ here)
        """
        result = Event.update
        # FIX 1: message is a dict, not an ArgObject — use .get()
        updates = message.get('updates', [])
        name2block = self.screen['name2block']

        for upd in updates:
            path = upd.get('path', [])
            data = upd.get('data', {})

            if not path:
                result = Event.unknown_update
                continue

            if len(path) == 1:
                # FIX 2: path[0] is the block name; original had bare `block` (NameError)
                block_name = path[0]
                if block_name in name2block:
                    name2block[block_name] = data
                    # Re-index any nested blocks inside the replaced block
                    if isinstance(data, dict) and data.get('type') == 'block':
                        name2block.update(self._build_name2block([data]))
                else:
                    result = Event.unknown_update

            else:
                # path is [element_name, block_name, ...parent_blocks...]
                # deepest-first, matching server's find_path / strpath convention
                elem_name = path[0]
                block_name = path[1]

                if block_name not in name2block:
                    result = Event.unknown_update
                    continue

                block = name2block[block_name]
                found = False
                # FIX 3: data is a dict — use dict.update(); original tried .__dict__
                for el in self._iter_block_elements(block.get('value', [])):
                    if el.get('name') == elem_name:
                        el.update(data)
                        found = True
                        break
                if not found:
                    result = Event.unknown_update

        return result