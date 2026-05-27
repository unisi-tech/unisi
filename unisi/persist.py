# Copyright © 2024 UNISI Tech. All rights reserved.
import importlib
import json
import os
import sqlite3
import sys
import time

from .common import strpath
from .units import ChangedProxy, Unit

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    user_id TEXT,
    namespace TEXT,
    path TEXT,
    value TEXT,
    ts REAL,
    PRIMARY KEY(user_id, namespace, path)
)
"""

SKIP_RESTORE_KEYS = {'id', 'path', 'tag', 'parent', '__class__', '__origin_module__', *Unit.action_list}

TYPE_MODULES = (
    'unisi.units',
    'unisi.containers',
    'unisi.tables',
    'unisi.graphs',
)

_CLASS_CACHE = {}
_SKIP_JSON = object()


def _path_key(path):
    if isinstance(path, list | tuple):
        return strpath(path)
    return str(path)


def _screen_name(current_screen):
    screen = getattr(current_screen, 'screen', current_screen)
    return getattr(screen, 'name', getattr(current_screen, 'name', ''))


def _screen_module(current_screen):
    return getattr(current_screen, '__name__', getattr(current_screen, '__module__', ''))


def _class_for(saved_dict):
    class_name = saved_dict.get('__class__')
    type_name = saved_dict.get('type')
    cache_key = class_name or type_name

    if cache_key and cache_key in _CLASS_CACHE:
        return _CLASS_CACHE[cache_key]

    cls = None
    if class_name:
        for module_name in TYPE_MODULES:
            module = sys.modules.get(module_name) or importlib.import_module(module_name)
            cls = getattr(module, class_name, None)
            if cls:
                break

    if not cls:
        fallback = {
            'block': ('unisi.containers', 'Block'),
            'screen': ('unisi.containers', 'Screen'),
            'command': ('unisi.units', 'Button'),
            'uploader': ('unisi.units', 'Button'),
            'camera': ('unisi.units', 'Button'),
            'range': ('unisi.units', 'Range'),
            'switch': ('unisi.units', 'Switch'),
            'select': ('unisi.units', 'Select'),
            'radio': ('unisi.units', 'Select'),
            'tree': ('unisi.units', 'Tree'),
            'text': ('unisi.units', 'TextArea'),
            'html': ('unisi.units', 'HTML'),
            'image': ('unisi.units', 'Image'),
            'video': ('unisi.units', 'Video'),
            'sound': ('unisi.units', 'Sound'),
            'chart': ('unisi.units', 'Chart'),
            'table': ('unisi.tables', 'Table'),
            'graph': ('unisi.graphs', 'Graph'),
        }.get(type_name, ('unisi.units', 'Unit'))
        cls = getattr(sys.modules.get(fallback[0]) or importlib.import_module(fallback[0]), fallback[1])

    if cache_key:
        _CLASS_CACHE[cache_key] = cls
    return cls


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
        state.setdefault('__class__', type(value).__name__)
        if hasattr(value, '_origin_module'):
            state.setdefault('__origin_module__', value._origin_module)
        if path := _unit_path_key(value, parents):
            state['id'] = path
        return _json_ready(state, parents)
    if isinstance(value, list | tuple | set):
        return [item for item in (_json_ready(v, parents) for v in value) if item is not _SKIP_JSON]
    if isinstance(value, dict):
        data = {}
        for key, val in value.items():
            if isinstance(key, str) and key.startswith('_') and key not in ('__class__', '__origin_module__'):
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
    if isinstance(value, list):
        return [_rebuild_value(item, unit_map) for item in value]
    if isinstance(value, dict) and 'id' in value:
        path = _path_key(value['id'])
        existing_unit = unit_map.get(path)
        if existing_unit:
            _smart_apply_dict(existing_unit, value, unit_map)
            return existing_unit
        new_unit = rebuild_unit_from_dict(value, unit_map)
        unit_map[path] = new_unit
        return new_unit
    if isinstance(value, dict):
        return {key: _rebuild_value(item, unit_map) for key, item in value.items()}
    return value


def rebuild_unit_from_dict(saved_dict, unit_map=None):
    unit_map = unit_map if unit_map is not None else {}
    cls = _class_for(saved_dict)
    unit = cls.__new__(cls)
    object.__setattr__(unit, '_mark_changed', None)
    object.__setattr__(unit, '_origin_module', saved_dict.get('__origin_module__', ''))

    for key, value in saved_dict.items():
        if key in SKIP_RESTORE_KEYS:
            continue
        object.__setattr__(unit, key, _rebuild_value(value, unit_map))
    return unit


def _smart_apply_dict(unit, saved_dict, unit_map):
    for key, value in saved_dict.items():
        if key in SKIP_RESTORE_KEYS:
            continue
        object.__setattr__(unit, key, _rebuild_value(value, unit_map))


class Persist:
    @staticmethod
    def db_path_for(session_id):
        return os.path.join('users', f'{session_id}.db')

    @staticmethod
    def exists(session_id):
        return os.path.exists(Persist.db_path_for(session_id))

    def __init__(self, session_id):
        self.user_id = session_id
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
        screen_module = _screen_module(current_screen)
        screen = getattr(current_screen, 'screen', current_screen)
        parents = getattr(screen, '_parents', {})
        ts = time.time()
        rows = []

        for root_obj, serialized_dict in persist_data:
            origin = getattr(root_obj, '_origin_module', '')
            namespace = origin if origin and origin != screen_module else screen_name
            path = _path_key(serialized_dict.get('id') or serialized_dict.get('path') or getattr(root_obj, 'name', ''))
            rows.append((self.user_id, namespace, path, json.dumps(_json_ready(serialized_dict, parents), ensure_ascii=False), ts))

        self.conn.executemany(
            'INSERT OR REPLACE INTO state(user_id, namespace, path, value, ts) VALUES (?, ?, ?, ?, ?)',
            rows,
        )
        self.conn.commit()

    def restore_screen(self, user, screen_module, screen_units):
        screen_name = _screen_name(screen_module)
        rows = self.conn.execute(
            """
            SELECT namespace, path, value, ts
            FROM state
            WHERE user_id = ?
              AND (namespace = ? OR namespace LIKE 'blocks.%')
            """,
            (self.user_id, screen_name),
        ).fetchall()

        if not rows:
            return

        local_state = {}
        shared_state = {}

        for namespace, path, value, ts in rows:
            try:
                state = json.loads(value)
            except json.JSONDecodeError:
                continue
            if namespace == screen_name:
                local_state[path] = state
            elif namespace.startswith('blocks.'):
                current = shared_state.get(path)
                if current is None or ts > current[1]:
                    shared_state[path] = (state, ts)

        unit_map = {}
        screen = getattr(screen_module, 'screen', screen_module)
        parents = getattr(screen, '_parents', {})
        for unit in screen_units:
            path = _unit_path_key(unit, parents)
            if path:
                unit_map[_path_key(path)] = unit

        paths_to_restore = set(local_state) | set(shared_state)
        sorted_paths = sorted(paths_to_restore, key=lambda p: p.count('@') + p.count('/'))
        timings = {}

        for path in sorted_paths:
            unit = unit_map.get(path)
            if unit:
                saved_dict = local_state.get(path)
                if path in shared_state:
                    shared_dict, shared_ts = shared_state[path]
                    for timing_path, timing_ts in timings.items():
                        if (path.startswith(timing_path + '@') or path.startswith(timing_path + '/')) and shared_ts < timing_ts:
                            saved_dict = shared_dict
                            break
                    else:
                        saved_dict = shared_dict
                    timings[path] = shared_ts
                if saved_dict:
                    _smart_apply_dict(unit, saved_dict, unit_map)


class UserPersistMixin:
    """Persist-related behaviour for User.
    Expects from host class:
      self.session, self.testing, self.screens,
      self.screen (property), self.screen_module,
      self._iter_units(), self.assign_parent_links(),
      self._global_persist (property, defined in User via config).
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
            any(getattr(u, 'persist', False) for u in self._iter_units(screen_module))

    def _has_persist_targets(self, screen, units):
        return self._persist_enabled() and (
            self._global_persist or getattr(screen, 'persist', False) or
            any(getattr(u, 'persist', False) for u in units))

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
        return getattr(unit, 'persist', False) or getattr(unit, '_persist', False)

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
                    if getattr(current, 'persist', False) or getattr(current, '_persist', False):
                        pr_obj = current
                        pr_path = fast_path(current)
                        break
                    current = getattr(self.screen, '_parents', {}).get(current)

            if pr_obj and pr_path:
                path_key = strpath(pr_path)
                if path_key not in persist_targets:
                    state = pr_obj.__getstate__()
                    state.setdefault('__class__', type(pr_obj).__name__)
                    if hasattr(pr_obj, '_origin_module'):
                        state.setdefault('__origin_module__', pr_obj._origin_module)
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