# Copyright © 2024 UNISI Tech. All rights reserved.
import ast
import importlib
import sys
import threading
from dataclasses import dataclass

from .containers import Screen
from .utils import blocks_dir, divpath, py_files, screens_dir


@dataclass
class ScreenInfo:
    name: str
    file: str
    icon: object = None
    order: int = 0


def _literal_assignments(path):
    values = {}
    try:
        with open(path, 'r') as file:
            tree = ast.parse(file.read(), filename=path)
    except (OSError, SyntaxError):
        return values

    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
                value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value

        if target in ('name', 'icon', 'order') and value is not None:
            try:
                values[target] = ast.literal_eval(value)
            except (ValueError, TypeError):
                pass
    return values


def screen_info_from_file(file):
    path = f'{screens_dir}{divpath}{file}'
    values = _literal_assignments(path)
    return ScreenInfo(
        name=values.get('name', ''),
        file=file,
        icon=values.get('icon', Screen.defaults.get('icon')),
        order=values.get('order', Screen.defaults.get('order', 0)),
    )


def screen_info_from_module(module):
    return ScreenInfo(
        name=getattr(module, 'name', ''),
        file=module.__file__.split(divpath)[-1],
        icon=getattr(module, 'icon', Screen.defaults.get('icon')),
        order=getattr(module.screen, 'order', getattr(module, 'order', Screen.defaults.get('order', 0))),
    )


class ModulesMixin:
    module_lock = threading.RLock()

    @classmethod
    def build_screen_registry(cls):
        registry = [screen_info_from_file(file) for file in py_files(screens_dir)]
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
        registry = [old for old in self.screen_registry if old.file != info.file]
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
            try:
                module = self.compile_screen(file)
            finally:
                self._capture_modules()
        return module

    def compile_screen(self, file):
        name = file[:-3]
        path = f'{screens_dir}{divpath}{file}'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        module.user = self

        spec.loader.exec_module(module)
        screen = Screen(getattr(module, 'name', ''))
        for var, val in screen.defaults.items():
            setattr(screen, var, getattr(module, var, val))
        if not isinstance(screen.blocks, list | tuple):
            screen.blocks = [screen.blocks]

        if self.__class__.toolbar and self.__class__.toolbar[0] not in screen.toolbar:
            screen.toolbar += self.__class__.toolbar

        screen._origin_module = module.__name__
        module.screen = screen
        self.assign_parent_links(module)
        if self.__class__.count > 0:
            screen.set_reactivity(self)
        self._upsert_screen_info(screen_info_from_module(module))
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
