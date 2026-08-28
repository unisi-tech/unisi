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
    # FIX: added, mirroring update_message/update_progress. A 'complete' or
    # 'append' response can legitimately bundle `updates` for OTHER units
    # that changed as a side effect of the same request (users.py's
    # prepare_result folds self.changed_units into any Message-typed raw
    # result -- not just the explicit 'update' type). Without these,
    # process() had no way to signal "this was complete/append AND it also
    # carried real local-state updates" the same way it already can for
    # message/progress -- see process()'s 'complete'/'append' branches.
    update_complete = 129
    update_append = 257
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
        session   : str  — optional session token to reattach to (sent as
                           the 'session' query parameter, matching
                           server.py's parsed_query['session'])
        screen    : str  — optional screen name to activate immediately on connect.
                           Mirrors the server-side User.__init__(screen=) parameter.
        """
        addr_port = f'{wss_header if ssl else ws_header}{host_port}'
        addr_port = f'{addr_port}{"" if addr_port.endswith("/") else "/"}{ws_path}'
        self.host_port = f'{"https" if ssl else "http"}://{host_port}'

        # Build query string: session token and/or initial screen name
        params = []
        if session:
            # FIX: was `params.append(session)` -- appended the raw token
            # with no 'session=' key at all (e.g. '?::1-0' instead of
            # '?session=%3A%3A1-0'), so server.py's
            # parse_qs(request.query_string) never saw a 'session' key
            # ('session' in parsed_query was always False) and silently
            # started a brand new session instead of reattaching to the
            # given one -- breaking the "Hot connect to running session"
            # feature (test_apps/proxy/run_blocks.py) entirely.
            params.append(f'session={quote(session, safe="")}')
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
        mtype = self.request(ArgObject(path=['root'], value=name))
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

    def _ancestor_chain(self, block_dict):
        """
        Return the ancestor chain for *block_dict* as a list: [block_dict's
        own name, *its parent's chain...], immediate block first. This is
        exactly path[1:] for any element living directly inside it (see
        find_path() server-side, which builds path the same way).
        """
        parent = self._find_parent_block(block_dict)
        name = block_dict['name']
        return [name] if parent is None else [name, *self._ancestor_chain(parent)]

    def _block_path(self, block_dict):
        """
        Return the '@'-joined display form of _ancestor_chain(block_dict),
        for the public block_name() API: 'inner@outer', deepest first.
        Top-level blocks return just their own name.
        """
        return '@'.join(self._ancestor_chain(block_dict))

    def _owning_block(self, element):
        """
        Return the *direct* containing block dict for *element*, searching
        the full nested tree.

        element : str  — match the first element found with this name.
                  dict — match this exact element object by identity.

        FIX: previously took only a name and always matched by name, even
        when block_name() already had the specific dict in hand (e.g. one
        entry from elements()). Two different elements on the same screen
        can legitimately share a name (element()'s own ambiguity check
        exists precisely because of this); matching by name alone meant
        whichever root block happened to be searched first always won,
        regardless of which object was actually passed in -- so a caller
        holding a specific, disambiguated dict could still be told it
        belongs to the wrong block. Matching by identity when a dict is
        given resolves it correctly; string lookups (no specific dict to
        compare against) keep the original by-name behaviour.

        Starts from root blocks so each element is found exactly once.
        """
        by_name = isinstance(element, str)

        def _matches(item):
            return item.get('name') == element if by_name else item is element

        def _search(block_dict):
            for item in flatten(block_dict.get('value', [])):
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'block':
                    found = _search(item)
                    if found is not None:
                        return found
                elif _matches(item):
                    return block_dict
            return None

        for root_block in self._root_blocks():
            found = _search(root_block)
            if found is not None:
                return found
        return None

    @staticmethod
    def _find_by_name_in_tree(value, target_name):
        """
        Recursively search *value* for a dict item named *target_name*, at
        any depth -- including block-typed items themselves, not only their
        descendants.

        Added for update()'s len(path) > 1 branch: unlike
        _iter_block_elements (which deliberately never yields a block dict
        itself, only recurses into it -- correct for element()/elements(),
        which must return leaf units, not containers), update() needs to
        find whatever is *named* by path[0], and that can itself be a
        nested block that changed as a whole (server's find_path returns
        [block.name, parent.name, ...] for a nested block exactly like it
        does [elem.name, block.name, ...] for a plain element -- see
        users.py). Matching only leaves meant such updates were silently
        unmatched.

        Returns the matching dict (the actual object living in the tree,
        so callers can mutate it in place and have that reflected
        everywhere it's referenced from, e.g. name2block), or None.
        """
        for item in flatten(value):
            if not isinstance(item, dict):
                continue
            if item.get('name') == target_name:
                return item
            if item.get('type') == 'block':
                found = Proxy._find_by_name_in_tree(item.get('value', []), target_name)
                if found is not None:
                    return found
        return None

    def _replace_root_block(self, block_name, new_data):
        """
        Replace the root-level block named *block_name* inside
        self.screen['blocks'] with *new_data*, in place.

        Added for update()'s len(path) == 1 branch, which otherwise only
        replaced name2block[block_name] (a flat index entry, rebound to a
        new dict object) without ever touching self.screen['blocks'] (the
        actual tree _root_blocks() -- and therefore elements()/commands/
        element() without a block_name/_owning_block -- reads). That left
        every tree-walking lookup serving the stale block indefinitely
        after a whole-root-block replacement, even though direct
        name2block-based lookups already saw the fresh data.

        self.screen['blocks'] is whatever json.loads produced, so any
        nested grouping is plain lists (JSON has no tuples) -- safe to
        mutate in place. No-op (leaves the tree untouched, same as before
        this fix existed) if *block_name* isn't found there -- update()'s
        own name2block-driven staleness check already governs whether this
        situation is even reachable.
        """
        def _replace(container):
            for i, item in enumerate(container):
                if isinstance(item, list):
                    if _replace(item):
                        return True
                elif isinstance(item, dict) and item.get('name') == block_name:
                    container[i] = new_data
                    return True
            return False

        _replace(self.screen.get('blocks', []))

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
            # FIX: guard self.screen being None (e.g. no screen received
            # yet, or the connection's first message was an error) -- was
            # `self.screen['name2block']` unguarded, raising TypeError
            # instead of gracefully returning None like the no-block_name
            # path below already does (via _root_blocks()'s own guard).
            if not self.screen:
                return None
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
        # FIX: pass element straight through (str or dict) instead of
        # pre-extracting just its name -- see _owning_block()'s docstring.
        owning = self._owning_block(element)
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
        # FIX: was `return self.interact(self.make_message(command, value))`
        # with no guard -- for an unknown command, make_message() returns
        # None, and interact(None)/request(None) would skip send() (message
        # is falsy) but still call conn.recv() unconditionally, desyncing
        # the request/response pairing (or hanging, against a real socket)
        # instead of failing fast. Mirrors set_value()'s existing guard.
        ms = self.make_message(command, value)
        return self.interact(ms) if ms else Event.invalid

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
        owning = self._owning_block(element)
        ancestors = self._ancestor_chain(owning) if owning is not None else []
        return ArgObject(
            path=[element['name'], *ancestors],
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
        return self.interact(ArgObject(path=[self.dialog['name']], value=command))

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
            # FIX: was `return Event.complete`, bypassing self.event = ...
            # entirely (self.event -- a documented, externally-read
            # attribute; see test_apps/proxy/run_blocks.py's own
            # `if proxy.event == ...` pattern -- stayed stuck on whatever
            # it was *before* this message). Also now applies `updates` if
            # present: a 'complete' response can legitimately bundle
            # updates for OTHER units that changed as a side effect of the
            # same request (users.py's prepare_result folds
            # self.changed_units into any Message-typed raw result, not
            # just the explicit 'update' type), and those were previously
            # dropped silently, same class of bug as 'append' below.
            updates = message.get('updates')
            if updates:
                self.update(message)
            self.event = Event.update_complete if updates else Event.complete

        elif mtype == 'append':
            # FIX: never looked at message['updates'] at all -- same
            # silently-dropped-side-effect-updates bug as 'complete' above.
            updates = message.get('updates')
            if updates:
                self.update(message)
            self.event = Event.update_append if updates else Event.append

        elif mtype == 'update':
            # FIX: was `self.update(message); self.event = Event.update`,
            # unconditionally -- discarding update()'s own verdict
            # (Event.unknown_update) whenever part of the batch couldn't be
            # matched against the local screen mirror, hiding a real
            # "your local copy may now be stale" signal from the caller.
            self.event = self.update(message)

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
          4. A root-block replacement (len(path) == 1) now also updates
             self.screen['blocks'] via _replace_root_block(), not just the
             separate name2block flat index (see that method's docstring).
          5. An element/nested-block replacement (len(path) > 1) now
             searches via _find_by_name_in_tree() instead of
             _iter_block_elements(), so a nested block that itself changed
             as a whole (not just a plain element inside one) can actually
             be found and updated (see that method's docstring).
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
                    # FIX 4: also keep self.screen['blocks'] itself (the
                    # tree _root_blocks()/elements()/commands/element()
                    # without a block_name actually read) in sync -- only
                    # name2block (a separate flat index) was updated
                    # before, so those tree-based lookups kept serving the
                    # stale block indefinitely. See _replace_root_block()'s
                    # docstring.
                    self._replace_root_block(block_name, data)
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
                # FIX 5: search via _find_by_name_in_tree, not
                # _iter_block_elements -- the latter deliberately never
                # yields a block dict itself (only recurses into it, which
                # is correct for element()/elements()'s "give me the leaf
                # units" contract), so whenever elem_name actually named a
                # nested block that changed as a whole (not a plain
                # element inside one), it could never be found and the
                # update was silently dropped as unknown_update, leaving
                # that block's content permanently stale.
                target = self._find_by_name_in_tree(block.get('value', []), elem_name)
                # FIX 3: data is a dict — use dict.update(); original tried .__dict__
                if target is not None:
                    target.update(data)
                    # Re-index in case the (possibly block-typed) target
                    # gained/lost nested blocks of its own -- same
                    # reasoning as the len(path) == 1 branch above.
                    if target.get('type') == 'block':
                        name2block.update(self._build_name2block([target]))
                else:
                    result = Event.unknown_update

        return result