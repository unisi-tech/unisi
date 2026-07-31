# Copyright © 2024 UNISI Tech. All rights reserved.
import json
import os
import sqlite3
import time

from .common import strpath
from .units import ChangedProxy, Unit

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    namespace TEXT,
    path TEXT,
    context_key TEXT,
    value TEXT,
    ts REAL,
    PRIMARY KEY(namespace, path, context_key)
)
"""
# context_key = '' для классических persist=True записей (позиционное восстановление);
# для keyed-persist (persist = функция) context_key = json от tuple определяющих значений;
# для простого get_key/set_key/get_keys/remove_key/remove_keys namespace и path тоже
# пустые, context_key = сам ключ (get_keys/remove_keys ищут по context_key через
# LIKE-паттерн, построенный из шаблона 'ab..ba').

# 'id' is the live matching key _rebuild_value/_smart_apply_dict use to find the
# existing unit a saved dict belongs to — restore would misbehave without it.
# Unit.action_list ('changed', 'complete', ...) must never be overwritten by restore.
SKIP_RESTORE_KEYS = {'id', *Unit.action_list}

_SKIP_JSON = object()
_UNRESOLVED = object()  # marks a saved unit reference with no live counterpart, so it gets dropped rather than fabricated
_NOT_FOUND = object()   # marks: no row saved for this (namespace, path, context_key)
_NO_KEY = object()      # marks: a keyed-persist unit whose key has never been computed yet


def _is_flag_persist(value):
    """True only for the classic boolean persist=True; excludes persist key-functions.
    A callable `persist` is a different mechanism (see UserPersistMixin.sync_keyed_persist)
    and must never be treated as the old positional persist=True flag."""
    return bool(value) and not callable(value)


def _effective_persist_key_fn(unit, parents):
    """The key-function governing `unit`'s keyed persistence: its own callable
    `persist`, or — if it doesn't have one — the nearest ancestor block/ParamBlock's
    callable `persist`. This is what persist=<function> set on a Block/ParamBlock
    means: a default handed down to its (possibly dynamically generated) leaf
    elements, each persisted individually. A container itself is never a persist
    target — see sync_keyed_persist for why."""
    current = unit
    while current is not None:
        p = getattr(current, 'persist', None)
        if callable(p):
            return p
        current = parents.get(current)
    return None


def _path_key(path):
    if isinstance(path, list | tuple):
        return strpath(path)
    return str(path)


def _screen_name(current_screen):
    screen = getattr(current_screen, 'screen', current_screen)
    return getattr(screen, 'name', getattr(current_screen, 'name', ''))


_LIKE_ESCAPE = '\\'


def _escape_like(fragment):
    """Escape SQL LIKE wildcards ('%', '_') and the escape char itself, so a
    literal template fragment is matched verbatim rather than as a pattern."""
    return (fragment
            .replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
            .replace('%', _LIKE_ESCAPE + '%')
            .replace('_', _LIKE_ESCAPE + '_'))


def _split_template(template):
    """Validate and split a get_keys()/remove_keys() template into (prefix, suffix).
    '..' marks the wildcard gap; prefix/suffix are literal text anchored to
    the start/end of the key: 'ab..' (prefix only), '..ba' (suffix only),
    'ab..ba' (both). Raises ValueError if `template` doesn't contain '..'."""
    if '..' not in template:
        raise ValueError("key template must contain '..', e.g. 'ab..', '..ba' or 'ab..ba'")
    prefix, _, suffix = template.partition('..')
    return prefix, suffix


def _template_to_like(template):
    """Turn a get_keys() template into an escaped SQL LIKE pattern (see _split_template)."""
    prefix, suffix = _split_template(template)
    return _escape_like(prefix) + '%' + _escape_like(suffix)


def _unit_path_key(unit, parents):
    path = []
    current = unit
    while current:
        name = getattr(current, 'name', None)
        if name:
            path.append(name)
        parent = parents.get(current)
        if parent is None:
            return None
        if getattr(parent, 'type', None) == 'screen':
            if current in getattr(parent, 'toolbar', ()):
                path.append('toolbar')
            return strpath(path[::-1])
        current = parent
    return None


def _json_ready(value, parents):
    if isinstance(value, ChangedProxy):
        value = value._obj
    if isinstance(value, Unit):
        state = value.__getstate__()
        if path := _unit_path_key(value, parents):
            state['id'] = path
        return _json_ready(state, parents)
    if isinstance(value, list | tuple | set):
        return [item for item in (_json_ready(v, parents) for v in value) if item is not _SKIP_JSON]
    if isinstance(value, dict):
        data = {}
        for key, val in value.items():
            if isinstance(key, str) and key.startswith('_'):
                continue
            item = _json_ready(val, parents)
            if item is not _SKIP_JSON:
                data[key] = item
        return data
    if callable(value) or isinstance(value, bytes):
        return _SKIP_JSON
    if hasattr(value, '__getstate__') and not isinstance(value, type):
        state = value.__getstate__()
        if isinstance(state, dict):
            return _json_ready(state, parents)
    if hasattr(value, '__dict__') and not isinstance(value, type):
        if type(value).__name__ in ('User', 'Persist'):
            return _SKIP_JSON
        return _json_ready(value.__dict__, parents)
    if value is None or isinstance(value, int | float | bool | str):
        return value
    return str(value)


def _rebuild_value(value, unit_map):
    """Resolve saved data against the live unit tree. A unit is only ever matched
    by id and updated in place; a saved id with no live counterpart is dropped —
    restore can update existing units but never creates or deletes them."""
    if isinstance(value, list):
        rebuilt = (_rebuild_value(item, unit_map) for item in value)
        return [item for item in rebuilt if item is not _UNRESOLVED]
    if isinstance(value, dict) and 'id' in value:
        existing_unit = unit_map.get(_path_key(value['id']))
        if existing_unit:
            _smart_apply_dict(existing_unit, value, unit_map)
            return existing_unit
        return _UNRESOLVED
    if isinstance(value, dict):
        return {key: _rebuild_value(item, unit_map) for key, item in value.items()}
    return value


def _smart_apply_dict(unit, saved_dict, unit_map):
    for key, value in saved_dict.items():
        if key in SKIP_RESTORE_KEYS:
            continue
        rebuilt = _rebuild_value(value, unit_map)
        if rebuilt is not _UNRESOLVED:
            object.__setattr__(unit, key, rebuilt)


class Persist:
    @staticmethod
    def db_path_for(session_id):
        return os.path.join('users', f'{session_id}.db')

    @staticmethod
    def exists(session_id):
        return os.path.exists(Persist.db_path_for(session_id))

    def __init__(self, session_id):
        self.db_path = self.db_path_for(session_id)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def save_changed(self, current_screen, persist_data):
        if not persist_data:
            return

        screen_name = _screen_name(current_screen)
        screen = getattr(current_screen, 'screen', current_screen)
        parents = getattr(screen, '_parents', {})
        ts = time.time()
        rows = []

        for _, serialized_dict in persist_data:
            path = _path_key(serialized_dict['id'])
            rows.append((screen_name, path, '', json.dumps(_json_ready(serialized_dict, parents), ensure_ascii=False), ts))

        self.conn.executemany(
            'INSERT OR REPLACE INTO state(namespace, path, context_key, value, ts) VALUES (?, ?, ?, ?, ?)',
            rows,
        )
        self.conn.commit()

    def restore_screen(self, user, screen_module, screen_units):
        screen_name = _screen_name(screen_module)
        rows = self.conn.execute(
            "SELECT path, value FROM state WHERE namespace = ? AND context_key = ''",
            (screen_name,),
        ).fetchall()

        if not rows:
            return

        unit_map = {}
        screen = getattr(screen_module, 'screen', screen_module)
        parents = getattr(screen, '_parents', {})
        for unit in screen_units:
            path = _unit_path_key(unit, parents)
            if path:
                unit_map[_path_key(path)] = unit

        # broader (shallower) persist targets first, so a more specific saved
        # entry applied afterwards correctly wins for its own subtree
        rows.sort(key=lambda row: row[0].count('@'))

        for path, value in rows:
            unit = unit_map.get(path)
            if not unit:
                continue
            try:
                saved_dict = json.loads(value)
            except json.JSONDecodeError:
                continue
            if saved_dict:
                _smart_apply_dict(unit, saved_dict, unit_map)

    def lookup_keyed(self, namespace, path, context_key):
        """Look up a value saved under a (namespace, path, context_key) triple.
        Used both by keyed-persist units and by the plain get_key/set_key API
        (which call with namespace='', path='')."""
        row = self.conn.execute(
            'SELECT value FROM state WHERE namespace = ? AND path = ? AND context_key = ?',
            (namespace, path, context_key),
        ).fetchone()
        if row is None:
            return _NOT_FOUND
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return _NOT_FOUND

    def lookup_keys(self, namespace, path, template):
        """Look up every value whose context_key matches `template` (see
        _template_to_like) within the given (namespace, path). Used by the
        plain get_keys() API (namespace='', path=''). Returns a
        {context_key: value} dict, empty if nothing matches."""
        like_pattern = _template_to_like(template)
        rows = self.conn.execute(
            'SELECT context_key, value FROM state '
            'WHERE namespace = ? AND path = ? AND context_key LIKE ? ESCAPE ?',
            (namespace, path, like_pattern, _LIKE_ESCAPE),
        ).fetchall()
        found = {}
        for context_key, value in rows:
            try:
                found[context_key] = json.loads(value)
            except json.JSONDecodeError:
                continue
        return found

    def save_keyed(self, namespace, path, context_key, value):
        """Save a value under a (namespace, path, context_key) triple.
        `value` must already be JSON-ready (run _json_ready first if it may
        contain Unit/ChangedProxy instances)."""
        if isinstance(value, ChangedProxy):
            value = value._obj
        self.conn.execute(
            'INSERT OR REPLACE INTO state(namespace, path, context_key, value, ts) VALUES (?, ?, ?, ?, ?)',
            (namespace, path, context_key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    def remove_keyed(self, namespace, path, context_key):
        """Delete the row at (namespace, path, context_key), if any. Returns
        the value that was stored there, or _NOT_FOUND if there was none.
        Used by the plain get_key/set_key/remove_key API (namespace='', path='')."""
        found = self.lookup_keyed(namespace, path, context_key)
        if found is not _NOT_FOUND:
            self.conn.execute(
                'DELETE FROM state WHERE namespace = ? AND path = ? AND context_key = ?',
                (namespace, path, context_key),
            )
            self.conn.commit()
        return found

    def remove_keys(self, namespace, path, template):
        """Delete every row whose context_key matches `template` (see
        _template_to_like) within the given (namespace, path). Used by the
        plain remove_keys() API (namespace='', path=''). Returns a
        {context_key: value} dict of everything that was removed, empty if
        nothing matched."""
        found = self.lookup_keys(namespace, path, template)
        if found:
            self.conn.executemany(
                'DELETE FROM state WHERE namespace = ? AND path = ? AND context_key = ?',
                [(namespace, path, context_key) for context_key in found],
            )
            self.conn.commit()
        return found


class UserPersistMixin:
    """Persist-related behaviour for User.
    Expects from host class:
      self.session, self.testing, self.screens,
      self.screen (property), self.screen_module,
      self._iter_units(), self.assign_parent_links(),
      self._global_persist (property, defined in User via config),
      self.changed_units, self.touched_units, self.register_changed_unit(), self.log()
      (the last four are needed by sync_keyed_persist / get_key / set_key).
    """

    def _init_persist(self):
        self.db = None
        self._screen_has_persist = False

    def _persist_enabled(self):
        return not self.testing

    def _persist_db(self, create=False):
        if not self._persist_enabled():
            return None
        if self.db is None:
            if not create and not Persist.exists(self.session):
                return None
            self.db = Persist(self.session)
        return self.db

    def _screen_has_persist_targets(self, screen_module=None):
        if not screen_module or not self._persist_enabled():
            return False
        screen = getattr(screen_module, 'screen', screen_module)
        return self._global_persist or getattr(screen, 'persist', False) or \
            any(_is_flag_persist(getattr(u, 'persist', False)) for u in self._iter_units(screen_module))

    def _has_persist_targets(self, screen, units):
        return self._persist_enabled() and (
            self._global_persist or getattr(screen, 'persist', False) or
            any(_is_flag_persist(getattr(u, 'persist', False)) for u in units))

    def _mark_persist_units(self):
        """Set _persist=True on every unit that appears in at least one persist screen.
        Called once after all screens are loaded (block modules still in sys.modules).
        Uses object.__setattr__ so the flag stays out of serialization (_-prefix).
        """
        for screen_module in self.screens:
            screen = getattr(screen_module, 'screen', screen_module)
            if getattr(screen, 'persist', False) or self._global_persist:
                for unit in self._iter_units(screen_module):
                    object.__setattr__(unit, '_persist', True)

    def _restore_persist_screen(self, screen_module):
        screen = getattr(screen_module, 'screen', screen_module)
        screen_units = list(self._iter_units(screen_module))
        has_persist = self._has_persist_targets(screen, screen_units)
        # Also restore if the screen contains shared-block units marked _persist
        has_shared = not has_persist and any(
            getattr(u, '_persist', False) for u in screen_units)
        if has_persist or has_shared:
            if db := self._persist_db(create=False):
                db.restore_screen(self, screen_module, screen_units)
            self.assign_parent_links(screen_module)
        return has_persist

    def _unit_has_persist_screen(self, unit):
        """True if unit should be persisted: explicit persist flag or marked via _persist."""
        return _is_flag_persist(getattr(unit, 'persist', False)) or getattr(unit, '_persist', False)

    def _collect_persist_data(self, units):
        if not units:
            return []
        persist_targets = {}
        screen_persist = self._global_persist or getattr(self.screen, 'persist', False)

        def fast_path(unit):
            if unit is self.screen:
                return None
            parents = getattr(self.screen, '_parents', {})
            path = []
            current = unit
            reached_screen = False
            while current:
                name = getattr(current, 'name', None)
                if name:
                    path.append(name)
                parent = parents.get(current)
                if parent is self.screen:
                    reached_screen = True
                    if current in getattr(self.screen, 'toolbar', ()):
                        path.append('toolbar')
                    break
                current = parent
            return path[::-1] if reached_screen and path else None

        for unit in units:
            if callable(getattr(unit, 'persist', None)) or \
                    _effective_persist_key_fn(unit, getattr(self.screen, '_parents', {})):
                continue  # keyed-persist units (own or inherited) are saved by sync_keyed_persist, not here

            path = fast_path(unit)
            if not path:
                continue

            pr_obj = None
            pr_path = None
            if screen_persist:
                pr_obj = unit
                pr_path = path
            else:
                current = unit
                while current:
                    if _is_flag_persist(getattr(current, 'persist', False)) or getattr(current, '_persist', False):
                        pr_obj = current
                        pr_path = fast_path(current)
                        break
                    current = getattr(self.screen, '_parents', {}).get(current)

            if pr_obj and pr_path:
                path_key = strpath(pr_path)
                if path_key not in persist_targets:
                    state = pr_obj.__getstate__()
                    state['id'] = path_key
                    persist_targets[path_key] = (pr_obj, state)

        return list(persist_targets.values())

    def _save_persist_if_needed(self, persist_units):
        """Save changed persist units to DB. Called at the end of prepare_result."""
        should_persist = self._screen_has_persist or (
            self._persist_enabled() and any(
                self._unit_has_persist_screen(u) for u in persist_units))
        if should_persist:
            persist_data = self._collect_persist_data(persist_units)
            if persist_data:
                if db := self._persist_db(create=True):
                    db.save_changed(self.screen_module, persist_data)

    def get_key(self, key: str):
        """Simple persistent key-value get, independent of screen/unit context.
        Uses the same `state` table/mechanism as unit persistence."""
        if db := self._persist_db(create=False):
            found = db.lookup_keyed('', '', key)
            if found is not _NOT_FOUND:
                return found
        return None

    def set_key(self, key: str, value: str):
        """Simple persistent key-value set, independent of screen/unit context."""
        if db := self._persist_db(create=True):
            db.save_keyed('', '', key, value)

    def get_keys(self, template: str):
        """Search simple persistent keys (the get_key/set_key store) by template.
        `template` must contain '..', which marks where any text may appear:
          'ab..'   -> keys starting with the literal text 'ab'
          '..ba'   -> keys ending with the literal text 'ba'
          'ab..ba' -> keys starting with 'ab' and ending with 'ba'
        Returns {key: value} for every match — empty dict if nothing matches,
        or if no key was ever persisted for this session yet.
        Raises ValueError if `template` doesn't contain '..'."""
        _split_template(template)  # validate eagerly, even before any DB file exists
        if db := self._persist_db(create=False):
            return db.lookup_keys('', '', template)
        return {}

    def remove_key(self, key: str):
        """Delete a simple persistent key (see get_key/set_key). Returns the
        value that was stored under `key`, or None if it didn't exist."""
        if db := self._persist_db(create=False):
            found = db.remove_keyed('', '', key)
            if found is not _NOT_FOUND:
                return found
        return None

    def remove_keys(self, template: str):
        """Delete every simple persistent key matching `template` (same
        format as get_keys — 'ab..', '..ba', 'ab..ba'). Returns {key: value}
        for everything that was removed — empty dict if nothing matched, or
        if no key was ever persisted for this session yet.
        Raises ValueError if `template` doesn't contain '..'."""
        _split_template(template)  # validate eagerly, even before any DB file exists
        if db := self._persist_db(create=False):
            return db.remove_keys('', '', template)
        return {}

    def _invalidate_keyed_persist_cache(self, screen_module=None):
        """Call this only if keyed-persist units are added to a screen dynamically at
        runtime (e.g. new rows spawning their own persist-bearing units). The normal
        case — a fixed set of fields declared once at screen build time — needs no call
        here at all; the cache is built lazily and correctly on first use."""
        screen = getattr(screen_module, 'screen', screen_module) if screen_module else self.screen
        object.__setattr__(screen, '_keyed_persist_cache', None)

    def _is_message_target(self, unit):
        """True if `unit` is the element the current incoming message is directly
        about — i.e. the client just edited it and already knows its state."""
        m = self.last_message
        return bool(m) and m.element == unit.name

    def _set_persist_active(self, unit, value):
        """Set unit.active — silently (no client notification) if the client is, in
        this very message, directly editing this same unit.
        register_changed_unit's echo check only suppresses a property when it matches
        the incoming message's own event/value; `active` never does (its event name is
        'active', never 'changed'), so a plain `unit.active = value` here would add the
        unit to changed_units and push an unsolicited update for the field someone is
        mid-keystroke on — the client applies it as a whole-unit refresh and the input
        resets. Applying it silently still lands the value (and it still reaches the
        client the next time this unit is included in an update for any other reason,
        e.g. its key next changes) without disturbing the field being typed into."""
        if getattr(unit, 'active', None) is value:
            return
        if self._is_message_target(unit):
            object.__setattr__(unit, 'active', value)
        else:
            unit.active = value

    def sync_keyed_persist(self):
        """For every LEAF unit (never a Block/ParamBlock) whose effective persist key
        function is set — its own `persist`, or one inherited from the nearest
        ancestor container (see _effective_persist_key_fn) — recompute the key.
          - key changed -> look up a saved override; if found, apply it to that one
            unit (active=True); if not, leave its current value alone (active=False).
          - key unchanged but the unit was touched/changed this cycle -> save its
            current state under that key (active=True).

        Containers (Block/ParamBlock) are never saved or restored as a whole:
          - their live child objects carry event handlers bound by business logic,
            which a generic mechanism cannot serialize or reconstruct;
          - marking a container "changed" pushes a whole-block update to an already
            rendered client, which re-renders the block wholesale — on every
            keystroke inside it, that means lost focus and the just-typed character.
        Populating a container's fields (e.g. from a selected table row) stays
        business logic's job. persist=<function> on a container is only a
        convenience default: each of its (possibly dynamically generated) leaf
        fields is still persisted individually, exactly like any standalone Unit —
        looked up by its own tree path, which stays stable across rebuilds because
        it's name-based, not object-identity-based.

        Must run before prepare_result builds persist_units/the response Message, so
        an applied change reaches the client in the same round-trip.
        """
        if not self._persist_enabled() or not self.screen_module:
            return
        screen = self.screen
        if getattr(screen, '_parents', None) is None:
            self.assign_parent_links()
        parents = screen._parents

        cache = getattr(screen, '_keyed_persist_cache', None)
        if cache is None:
            keyed_units, unit_map = [], {}
            for u in self._iter_units():
                p = _unit_path_key(u, parents)
                if p:
                    unit_map[_path_key(p)] = u
                if getattr(u, 'type', None) == 'block':
                    continue  # containers are never persist targets themselves
                if key_fn := _effective_persist_key_fn(u, parents):
                    keyed_units.append((u, key_fn))
            cache = (keyed_units, unit_map)
            object.__setattr__(screen, '_keyed_persist_cache', cache)
        keyed_units, unit_map = cache
        if not keyed_units:
            return

        namespace = _screen_name(self.screen_module)
        touched = self.changed_units | self.touched_units

        for unit, key_fn in keyed_units:
            try:
                new_key = key_fn()
            except Exception as e:
                self.log(f'persist key function failed for "{unit.name}": {e}', type='warning')
                continue
            if not isinstance(new_key, list | tuple):
                new_key = (new_key,)
            new_key = tuple(new_key)

            path = _unit_path_key(unit, parents)
            if not path:
                continue
            context_key = json.dumps(list(new_key), ensure_ascii=False)

            if new_key != getattr(unit, '_persist_key', _NO_KEY):
                object.__setattr__(unit, '_persist_key', new_key)
                db_ro = self._persist_db(create=False)
                found = db_ro.lookup_keyed(namespace, path, context_key) if db_ro else _NOT_FOUND
                if found is not _NOT_FOUND and isinstance(found, dict):
                    _smart_apply_dict(unit, found, unit_map)
                    if not self._is_message_target(unit):
                        self.register_changed_unit(unit)  # applied silently via object.__setattr__ above — mark dirty so the diff reaches the client
                    self._set_persist_active(unit, True)
                else:
                    self._set_persist_active(unit, False)
            elif unit in touched:
                state = unit.__getstate__()
                state['id'] = path
                if db_rw := self._persist_db(create=True):
                    db_rw.save_keyed(namespace, path, context_key, _json_ready(state, parents))
                self._set_persist_active(unit, True)