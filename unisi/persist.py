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