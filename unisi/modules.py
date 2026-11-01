# Copyright © 2024 UNISI Tech. All rights reserved.
import ast
import builtins
import copy
import importlib
import re
import sys
import threading
from dataclasses import dataclass

from .containers import Screen
from .utils import app_path, blocks_dir, divpath, py_files, screens_dir

_no_user = object()


@dataclass
class ScreenInfo:
    name: str
    file: str
    icon: object = None
    order: int = 0


def screen_info_from_module(module):
    return ScreenInfo(
        name=getattr(module, 'name', ''),
        file=module.__file__.split(divpath)[-1],
        icon=getattr(module, 'icon', Screen.defaults.get('icon')),
        order=getattr(module.screen, 'order', getattr(module, 'order', Screen.defaults.get('order', 0))),
    )


# what a screen declares of itself starts a line: `name = 'Home'`
_declaration = re.compile(rb'^(?:name|icon|order)(?![A-Za-z0-9_])', re.MULTILINE)


def declared_screen_info(file):
    """What a screen file declares at its module level as plain literals -- `name = 'Home'`,
    `icon = 'home'`, `order = 1` -- read without running the file. Whatever is computed, or
    not of the right type, is left out: it becomes known when the screen is loaded.

    A declaration is a statement that starts a line with the name (see declared_in). The registry
    asks every screen file at the first session of the server, and a screen is long while its
    declarations are at its top, so what is parsed is the head of the file up to the last line that
    starts with one of the three names; the whole file is parsed only when that head ends inside a
    statement (a declaration that goes on in the next lines, a string that is still open)."""
    path = app_path(screens_dir, file)
    try:
        with open(path, 'rb') as source:
            data = source.read().removeprefix(b'\xef\xbb\xbf')
    except OSError:
        return {}
    last = None
    for last in _declaration.finditer(data):
        pass
    if last is None:
        return {}
    end = data.find(b'\n', last.end()) + 1      # 0: the line is the last one and has no line end
    for part in (data[:end], data) if 0 < end < len(data) else (data,):
        try:
            return declared_in(ast.parse(part, path))
        except (SyntaxError, ValueError):
            pass
    return {}


def declared_in(tree):
    """The `name`, `icon` and `order` the top level of a module assigns as plain literals, in the
    statements that start a line with the name: not `x = name = 'Home'` or `a = 1; name = 'Home'`."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in ('name', 'icon', 'order') and not target.col_offset:
            if isinstance(value, ast.Constant):
                found[target.id] = value.value
            else:
                found.pop(target.id, None)
    types = dict(name=str, icon=(str, type(None)), order=(int, float))
    return {key: value for key, value in found.items() if isinstance(value, types[key]) and not isinstance(value, bool)}


class ModulesMixin:
    module_lock = threading.RLock()

    @classmethod
    def build_screen_registry(cls):
        # The screens are not loaded yet, but what their files declare is known: the menu shows
        # their names, they can be found by them, and the first of the registry -- the screen every
        # new session starts at -- is the one of the lowest order. The files are taken sorted, as the
        # listing order of a directory depends on the file system.
        registry = []
        for file in sorted(py_files(app_path(screens_dir))):
            declared = declared_screen_info(file)
            registry.append(ScreenInfo(name=declared.get('name', ''), file=file,
                icon=declared.get('icon', Screen.defaults.get('icon')),
                order=declared.get('order', Screen.defaults.get('order', 0))))
        registry.sort(key=lambda info: info.order)
        return registry

    def _init_screen_registry(self):
        if not getattr(self.__class__, '_screen_registry_ready', False):
            self.__class__.screen_registry = self.build_screen_registry()
            self.__class__._screen_registry_ready = True

    def _screen_info(self, name):
        for info in self.screen_registry:
            if name in (info.name, info.file, info.file[:-3]):
                return info

    def _upsert_screen_info(self, info):
        # A known screen keeps its place among the screens of the same order. The ones not loaded
        # yet are placeholders of order 0, so appending the screen just loaded behind them (and
        # only then sorting) would move it after them: the next session would start at another
        # screen than this one did, and so would each of the following ones, until all were loaded.
        registry, placed = [], False
        for old in self.screen_registry:
            if old.file != info.file:
                registry.append(old)
            elif not placed:
                registry.append(info)
                placed = True
        if not placed:
            registry.append(info)
        registry.sort(key=lambda item: item.order)
        self.__class__.screen_registry = registry
        self.__class__._screen_registry_ready = True

    def _remove_screen_info(self, file):
        self.__class__.screen_registry = [
            info for info in self.screen_registry if info.file != file
        ]
        self.__class__._screen_registry_ready = True

    def _remove_module(self, name):
        module = sys.modules.get(name)
        parent_name, _, child_name = name.rpartition('.')
        parent = sys.modules.get(parent_name)
        if parent and getattr(parent, child_name, None) is module:
            try:
                delattr(parent, child_name)
            except AttributeError:
                pass
        sys.modules.pop(name, None)

    def _install_modules(self):
        for name in list(sys.modules):
            if name.startswith(f'{blocks_dir}.'):
                self._remove_module(name)
        sys.modules.update(self.modules)
        for name, module in self.modules.items():
            parent_name, _, child_name = name.rpartition('.')
            if parent_name:
                parent = sys.modules.get(parent_name) or importlib.import_module(parent_name)
                setattr(parent, child_name, module)

    def _capture_modules(self):
        for name in [name for name in sys.modules if name.startswith(f'{blocks_dir}.')]:
            module = sys.modules[name]
            module.user = self
            self.modules[name] = module
            self._remove_module(name)

    def _drop_private_module(self, name):
        self.modules.pop(name, None)
        self._remove_module(name)

    def set_clean(self):
        """Capture and remove this user's block modules from sys.modules."""
        self._capture_modules()

    def load_screen(self, file):
        with self.module_lock:
            self._install_modules()
            outer = builtins.__dict__.get('user', _no_user)
            builtins.user = self        # a block the screen imports sees its user while it runs, like the screen
            try:
                module = self.compile_screen(file)
            finally:
                if outer is _no_user:
                    del builtins.user
                else:
                    builtins.user = outer
                self._capture_modules()
        return module

    def compile_screen(self, file):
        name = file[:-3]
        path = app_path(screens_dir, file)   # absolute once the server runs (cwd may be borrowed)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        module.user = self

        spec.loader.exec_module(module)
        screen = Screen(getattr(module, 'name', ''))
        for var, val in screen.defaults.items():
            resolved = getattr(module, var, val)
            # Screen.defaults (see unisi/utils.py) is a SINGLE dict built once
            # at import time -- its list-valued entries ('blocks', 'toolbar')
            # are specific, shared list objects, not fresh ones per screen.
            # `resolved is val` means the module didn't declare its own value
            # for `var`, so we're about to fall back to that shared object.
            # Handing it out by reference would let the `screen.toolbar +=
            # ...` mutation a few lines down (or any other later in-place
            # mutation of screen.blocks/screen.toolbar, anywhere in the
            # codebase) permanently corrupt the shared default for every
            # OTHER screen that also doesn't declare its own -- copying here
            # means each screen gets its own list to mutate.
            if resolved is val and isinstance(val, (list, dict, set)):
                resolved = copy.copy(val)
            setattr(screen, var, resolved)
        if not isinstance(screen.blocks, list | tuple):
            screen.blocks = [screen.blocks]

        if self.__class__.toolbar and self.__class__.toolbar[0] not in screen.toolbar:
            screen.toolbar += self.__class__.toolbar

        module.screen = screen
        self.assign_parent_links(module)
        
        return module

    def _finish_loaded_screen(self, module, prepare=False):
        self._mark_persist_units()
        self._restore_persist_screen(module)
        self._screen_has_persist = self._screen_has_persist_targets(module)
        if prepare and hasattr(module, 'prepare'):
            module.prepare()
            self._screen_has_persist = self._screen_has_persist_targets(module)
        self.update_menu()

    def load_lazy(self, screen=None):
        if self.screens:
            module = self.ensure_screen(screen) if screen else self.screen_module
            self.update_menu()
            return bool(module or self.screens)

        if not self.screen_registry:
            return False

        info = self._screen_info(screen) if screen else self.screen_registry[0]
        if not info:
            return False

        module = self.load_screen(info.file)
        self.screens.append(module)
        self.screens.sort(key=lambda item: item.screen.order)
        self.screen_module = module
        self._finish_loaded_screen(module, prepare=True)
        return True

    def load(self, screen=None):
        return self.load_lazy(screen)

    def ensure_screen(self, name):
        if not name:
            return None
        for module in self.screens:
            if name in (getattr(module, 'name', None), module.screen.name, module.__file__.split(divpath)[-1], module.__name__):
                return module

        info = self._screen_info(name)
        if not info:
            return None

        module = self.load_screen(info.file)
        self.screens.append(module)
        self.screens.sort(key=lambda item: item.screen.order)
        self._finish_loaded_screen(module)
        return module