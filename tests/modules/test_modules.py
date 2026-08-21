"""
Unit tests for unisi/modules.py -- ModulesMixin, the screen registry, and
the per-user "private blocks" module system.

Deliberately does not re-cover message dispatch, multi-user fan-out, or
screen navigation dispatch (screen_process/set_screen) -- see tests/users/
for those. This file is about the mechanics UNDER that: how the registry of
available screens gets built and kept up to date, how a screen file gets
compiled into a live Screen object, how "already loaded" is detected, and
how blocks/*.py modules (imported BY a screen, shared by reference within
one user's session) get juggled through sys.modules around each compile.

Uses real User instances against fixtures_app/ -- same rationale as
tests/users/conftest.py and tests/persist_voice_reloder/conftest.py.
"""
import sys

import pytest

from unisi.modules import ScreenInfo, screen_info_from_module
from unisi.units import Button, Edit


# =============================================================================
# ScreenInfo / screen_info_from_module
# =============================================================================

class TestScreenInfo:
    def test_defaults(self):
        info = ScreenInfo(name="Home", file="home.py")
        assert info.icon is None
        assert info.order == 0

    def test_screen_info_from_module_reads_screen_order_over_module_order(self, make_user):
        user = make_user("alpha")
        module = user.screen_module
        # Screen.__init__ / compile_screen already copied module.order (0)
        # onto screen.order; screen_info_from_module prefers screen.order.
        info = screen_info_from_module(module)
        assert info.name == "Alpha"
        assert info.file == "alpha.py"
        assert info.order == 0

    def test_screen_info_from_module_file_has_no_directory_prefix(self, make_user):
        user = make_user("alpha")
        info = screen_info_from_module(user.screen_module)
        assert info.file == "alpha.py"  # not 'screens/alpha.py' or an absolute path


# =============================================================================
# build_screen_registry / _init_screen_registry
# =============================================================================

class TestBuildScreenRegistry:
    def test_finds_every_screen_file(self):
        from unisi.users import User
        registry = User.build_screen_registry()
        files = {info.file for info in registry}
        assert files == {"alpha.py", "beta.py", "uses_block.py"}

    def test_entries_start_with_a_blank_name_and_zero_order(self):
        """
        Documents real behaviour: build_screen_registry() only lists
        FILENAMES -- it doesn't compile anything, so it can't know a
        screen's declared `name` or `order` yet. Those get filled in only
        once the screen is actually loaded (compile_screen ->
        _upsert_screen_info). Until then, every entry is a blank
        placeholder with name='' and order=0, regardless of what the
        screen file itself declares.
        """
        from unisi.users import User
        registry = User.build_screen_registry()
        assert all(info.name == "" for info in registry)
        assert all(info.order == 0 for info in registry)

    def test_init_screen_registry_only_scans_once(self, registry_snapshot):
        from unisi.users import User
        User._screen_registry_ready = False
        User.screen_registry = []
        user = object.__new__(User)  # no need for a full User for this

        user._init_screen_registry()
        first = User.screen_registry
        assert User._screen_registry_ready is True

        # Mutate the class-level registry directly, then call again -- a
        # real rescan would blow this away; caching means it's preserved.
        User.screen_registry = ["sentinel"]
        user._init_screen_registry()
        assert User.screen_registry == ["sentinel"]


# =============================================================================
# _screen_info lookup
# =============================================================================

class TestScreenInfoLookup:
    def test_found_by_filename(self, make_user):
        user = make_user()
        info = user._screen_info("alpha.py")
        assert info is not None
        assert info.file == "alpha.py"

    def test_found_by_filename_without_extension(self, make_user):
        user = make_user()
        info = user._screen_info("alpha")
        assert info is not None
        assert info.file == "alpha.py"

    def test_not_found_by_declared_name_before_first_load(self, registry_snapshot):
        """
        Documents real behaviour, worth knowing: build_screen_registry()'s
        placeholder entries (see TestBuildScreenRegistry above) have a
        blank `name`, so a screen that has never been loaded/compiled by
        ANY user yet in this process can only be found by filename here --
        not by the name: string it declares internally -- until the first
        successful load fills that in via _upsert_screen_info.
        """
        from unisi.users import User
        User._screen_registry_ready = False
        User.screen_registry = User.build_screen_registry()  # fresh, nothing loaded yet
        user = object.__new__(User)

        assert user._screen_info("Alpha") is None  # declared name, not yet known
        assert user._screen_info("alpha.py") is not None  # filename always works

    def test_found_by_declared_name_after_a_load(self, make_user):
        # Loading (via any user, in this same process) fills in the real
        # name for every subsequent lookup, by anyone.
        make_user("beta")
        other_user = make_user()
        assert other_user._screen_info("Beta") is not None

    def test_unknown_name_returns_none(self, make_user):
        user = make_user()
        assert user._screen_info("NoSuchScreenAtAll") is None


# =============================================================================
# _upsert_screen_info / _remove_screen_info
# =============================================================================

class TestUpsertScreenInfo:
    def test_adds_a_new_entry(self, make_user, registry_snapshot):
        user = make_user()
        before = len(user.screen_registry)
        user._upsert_screen_info(ScreenInfo(name="Brand New", file="brand_new.py", order=99))
        assert len(user.screen_registry) == before + 1
        assert user._screen_info("Brand New").file == "brand_new.py"

    def test_replaces_the_existing_entry_for_the_same_file(self, make_user, registry_snapshot):
        user = make_user()
        user._upsert_screen_info(ScreenInfo(name="Alpha Take 1", file="alpha.py", order=5))
        user._upsert_screen_info(ScreenInfo(name="Alpha Take 2", file="alpha.py", order=7))

        matches = [info for info in user.screen_registry if info.file == "alpha.py"]
        assert len(matches) == 1
        assert matches[0].name == "Alpha Take 2"
        assert matches[0].order == 7

    def test_resorts_by_order(self, make_user, registry_snapshot):
        user = make_user()
        user._upsert_screen_info(ScreenInfo(name="Z", file="z.py", order=-1))
        assert user.screen_registry[0].file == "z.py"

    def test_marks_the_registry_ready(self, make_user, registry_snapshot):
        from unisi.users import User
        user = make_user()
        User._screen_registry_ready = False
        user._upsert_screen_info(ScreenInfo(name="X", file="x.py"))
        assert User._screen_registry_ready is True


class TestRemoveScreenInfo:
    def test_removes_the_matching_entry(self, make_user, registry_snapshot):
        user = make_user()
        assert user._screen_info("alpha.py") is not None
        user._remove_screen_info("alpha.py")
        assert user._screen_info("alpha.py") is None

    def test_leaves_other_entries_untouched(self, make_user, registry_snapshot):
        user = make_user()
        user._remove_screen_info("alpha.py")
        assert user._screen_info("beta.py") is not None

    def test_unknown_file_is_a_noop(self, make_user, registry_snapshot):
        user = make_user()
        before = list(user.screen_registry)
        user._remove_screen_info("does_not_exist.py")
        assert user.screen_registry == before


# =============================================================================
# Private "blocks" module system: _install_modules / _capture_modules /
# _remove_module / _drop_private_module / set_clean
# =============================================================================

class TestPrivateBlocksModules:
    def test_loading_a_screen_that_imports_a_block_captures_it_per_user(self, make_user):
        user = make_user("uses_block.py")
        assert "blocks.widget" in user.modules

    def test_sys_modules_is_clean_after_loading(self, make_user):
        # _capture_modules() moves blocks.* out of the GLOBAL sys.modules
        # and into this user's own `.modules` dict once compilation
        # finishes, so a DIFFERENT user's screen doesn't accidentally see
        # (or reuse) this one's block instances via the module cache.
        make_user("uses_block.py")
        assert not [n for n in sys.modules if n.startswith("blocks.")]

    def test_two_users_get_independent_block_instances(self, make_user):
        user_a = make_user("uses_block.py")
        user_b = make_user("uses_block.py")

        widget_a = user_a.modules["blocks.widget"].widget_block
        widget_b = user_b.modules["blocks.widget"].widget_block

        assert widget_a is not widget_b

    def test_drop_private_module_removes_it_from_user_modules(self, make_user):
        user = make_user("uses_block.py")
        assert "blocks.widget" in user.modules
        user._drop_private_module("blocks.widget")
        assert "blocks.widget" not in user.modules

    def test_drop_private_module_unknown_name_is_a_noop(self, make_user):
        user = make_user()
        user._drop_private_module("blocks.never_loaded")  # must not raise

    def test_set_clean_captures_currently_installed_modules(self, make_user):
        user = make_user("uses_block.py")
        # Simulate a block module having been left installed globally
        # (e.g. by code running outside the normal load_screen flow).
        widget_mod = user.modules.pop("blocks.widget")
        sys.modules["blocks.widget"] = widget_mod

        user.set_clean()

        assert "blocks.widget" in user.modules
        assert "blocks.widget" not in sys.modules

    def test_install_then_capture_round_trips_through_sys_modules(self, make_user):
        user = make_user("uses_block.py")
        assert "blocks.widget" not in sys.modules  # captured already

        user._install_modules()
        assert "blocks.widget" in sys.modules  # temporarily reinstalled
        assert sys.modules["blocks.widget"] is user.modules["blocks.widget"]

        user._capture_modules()
        assert "blocks.widget" not in sys.modules  # captured back out again

    def test_install_modules_evicts_a_different_users_stale_block_module(self, make_user):
        """
        _install_modules() removes ANY existing blocks.* entry from
        sys.modules before installing this user's own -- otherwise a
        leftover module object from a previous compile (this user's own
        earlier load, or -- if something ever left one installed -- another
        user's) would shadow the fresh one about to be exec'd.
        """
        user_a = make_user("uses_block.py")
        stale_widget = user_a.modules["blocks.widget"]
        sys.modules["blocks.widget"] = stale_widget  # simulate a leftover

        user_b = make_user("uses_block.py")

        assert user_b.modules["blocks.widget"] is not stale_widget


# =============================================================================
# compile_screen (base ModulesMixin behaviour)
# =============================================================================

class TestCompileScreen:
    def test_applies_screen_defaults_from_module_attributes(self, make_user):
        user = make_user("alpha")
        assert user.screen_module.screen.name == "Alpha"

    def test_assigns_parent_links(self, make_user):
        user = make_user("alpha")
        module = user.screen_module
        assert module.screen._parents.get(module.field) is module.screen.blocks[0]

    def test_class_level_toolbar_is_merged_in(self, make_user):
        from unisi.users import User
        User.toolbar = [Button("GlobalHelp")]

        user = make_user("beta")

        assert any(b.name == "GlobalHelp" for b in user.screen_module.screen.toolbar)

    def test_class_level_toolbar_not_duplicated_across_reloads_of_the_same_toolbar_objects(self, make_user):
        # The dedup check (`self.__class__.toolbar[0] not in screen.toolbar`)
        # correctly recognises the SAME button object across repeated
        # compiles, as long as User.toolbar itself keeps pointing at the
        # same list of button objects -- the realistic case (set once,
        # left alone).
        from unisi.users import User
        help_button = Button("GlobalHelp")
        User.toolbar = [help_button]

        user = make_user()
        module = user.load_screen("alpha.py")
        module2 = user.load_screen("alpha.py")

        assert len([b for b in module2.screen.toolbar if b.name == "GlobalHelp"]) == 1

    def test_reassigning_user_toolbar_to_new_button_objects_does_not_leak_into_the_shared_default(self, make_user):
        """
        Regression test for a fixed bug, found while writing this suite.

        unisi/utils.py builds Screen.defaults = dict(..., toolbar=[], ...)
        ONCE, at import time. compile_screen() used to fall back to that
        SAME shared list object, by reference, for every screen that
        doesn't declare its own `toolbar` -- and then
            screen.toolbar += self.__class__.toolbar
        mutated it IN PLACE. The dedup check that prevents re-adding the
        same button only compares by identity/equality against whatever's
        ALREADY in that shared list -- it had no way to know a *different*
        Button object it had never seen before (even one with the exact
        same name) was "the same" logical toolbar entry. So reassigning
        User.toolbar to a new list of new button objects (plausible under
        hot-reload, where screen/config code re-executes) permanently
        accumulated duplicates into Screen.defaults['toolbar'] itself --
        affecting every screen that doesn't declare its own toolbar, not
        just the one being compiled at the time.

        Fixed by copying the default's list/dict/set values before handing
        them out, whenever a screen falls back to one instead of declaring
        its own -- see compile_screen()'s own comment for the details.
        """
        from unisi.users import User
        from unisi.containers import Screen

        User.toolbar = [Button("GlobalHelp")]
        user = make_user()
        first_module = user.load_screen("alpha.py")

        User.toolbar = [Button("GlobalHelp")]  # reassigned to a brand new object
        second_module = user.load_screen("alpha.py")

        help_buttons = [b for b in second_module.screen.toolbar if b.name == "GlobalHelp"]
        assert len(help_buttons) == 1
        # the shared default itself was never mutated -- unaffected by
        # either compile, and unaffected by whatever runs after this test.
        assert Screen.defaults["toolbar"] == []
        # the first screen's own toolbar is a separate list, also untouched
        # by the second compile.
        assert len([b for b in first_module.screen.toolbar if b.name == "GlobalHelp"]) == 1

    def test_no_class_level_toolbar_leaves_screen_toolbar_as_declared(self, make_user):
        from unisi.users import User
        User.toolbar = []

        user = make_user("alpha")

        assert list(user.screen_module.screen.toolbar) == []


class TestCompileScreenEdgeCases:
    def test_single_block_not_wrapped_in_a_list_gets_wrapped_into_one(self, real_screens_dir_modules):
        (real_screens_dir_modules / "single.py").write_text(
            "from unisi import Block, Edit\n"
            "name = 'Single'\n"
            "blocks = Block('Root', Edit('X', '1'))\n"  # a bare Block, not [Block(...)]
        )
        from unisi.users import User
        user = User("scratch-single-block")

        module = user.compile_screen("single.py")

        assert len(module.screen.blocks) == 1
        assert list(module.screen.blocks)[0].name == "Root"


# =============================================================================
# load_screen
# =============================================================================

class TestLoadScreen:
    def test_returns_a_compiled_module(self, make_user):
        user = make_user()
        module = user.load_screen("alpha.py")
        assert module.screen.name == "Alpha"

    def test_captures_block_modules_after_loading(self, make_user):
        user = make_user()
        user.load_screen("uses_block.py")
        assert "blocks.widget" in user.modules
        assert not [n for n in sys.modules if n.startswith("blocks.")]

    def test_reloading_the_same_screen_produces_a_fresh_module_object(self, make_user):
        user = make_user()
        first = user.load_screen("alpha.py")
        second = user.load_screen("alpha.py")
        assert first is not second
        assert first.screen is not second.screen


# =============================================================================
# _finish_loaded_screen
# =============================================================================

class TestFinishLoadedScreen:
    def test_updates_the_menu(self, make_user):
        user = make_user("alpha")
        assert user.screen_module.screen.menu  # non-empty: built from the registry

    def test_prepare_true_calls_the_screen_module_prepare_function(self, make_user):
        user = make_user()
        module = user.ensure_screen("beta.py")
        module.prepared.clear()
        user._finish_loaded_screen(module, prepare=True)
        assert module.prepared == [1]

    def test_prepare_false_does_not_call_prepare(self, make_user):
        user = make_user()
        module = user.ensure_screen("beta.py")
        module.prepared.clear()
        user._finish_loaded_screen(module, prepare=False)
        assert module.prepared == []

    def test_prepare_true_on_a_screen_without_a_prepare_function_is_a_noop(self, make_user):
        user = make_user()
        module = user.ensure_screen("alpha.py")
        user._finish_loaded_screen(module, prepare=True)  # must not raise (Alpha has no prepare())


# =============================================================================
# load_lazy / load
# =============================================================================

class TestLoadLazy:
    def test_first_call_with_no_screen_param_loads_the_first_registry_entry(self, registry_snapshot):
        from unisi.users import User
        User._screen_registry_ready = False
        User.screen_registry = []
        user = User("scratch-load-lazy-1")
        assert user.screen_module is not None
        assert user.screens == [user.screen_module]

    def test_call_with_a_screen_param_loads_that_specific_screen(self):
        from unisi.users import User
        user = User("scratch-load-lazy-2", screen="beta.py")
        assert user.screen_module.screen.name == "Beta"

    def test_unknown_screen_param_on_first_load_leaves_no_screen(self):
        from unisi.users import User
        user = User("scratch-load-lazy-3", screen="NoSuchScreenAtAll")
        assert user.screen_module is None
        assert user.screens == []

    def test_already_has_screens_and_screen_param_switches_via_ensure_screen(self, make_user):
        user = make_user("alpha")
        result = user.load_lazy("beta.py")
        assert result is True
        assert any(s.screen.name == "Beta" for s in user.screens)

    def test_already_has_screens_and_no_screen_param_just_refreshes_menu(self, make_user):
        user = make_user("alpha")
        current = user.screen_module
        result = user.load_lazy(None)
        assert result is True
        assert user.screen_module is current  # unchanged

    def test_no_registry_at_all_returns_false(self, registry_snapshot):
        from unisi.users import User
        User._screen_registry_ready = True  # pretend it's "ready" but empty
        User.screen_registry = []
        user = object.__new__(User)
        user.screens = []
        user.screen_module = None
        assert user.load_lazy(None) is False

    def test_load_is_an_alias_for_load_lazy(self, make_user):
        user = make_user("alpha")
        calls = []
        user.load_lazy = lambda screen=None: calls.append(screen) or True

        result = user.load("beta.py")

        assert calls == ["beta.py"]
        assert result is True


# =============================================================================
# ensure_screen
# =============================================================================

class TestEnsureScreen:
    def test_empty_name_returns_none(self, make_user):
        user = make_user()
        assert user.ensure_screen(None) is None
        assert user.ensure_screen("") is None

    def test_loads_and_registers_a_not_yet_loaded_screen(self, make_user):
        user = make_user()
        assert user.screens == [] or all(s.screen.name != "Beta" for s in user.screens)
        module = user.ensure_screen("beta.py")
        assert module.screen.name == "Beta"
        assert module in user.screens

    def test_unknown_screen_returns_none(self, make_user):
        user = make_user()
        assert user.ensure_screen("TotallyUnknownScreen") is None

    def test_already_loaded_screen_is_returned_without_recompiling(self, make_user):
        user = make_user("alpha")
        first = user.screen_module
        again = user.ensure_screen("alpha.py")
        assert again is first  # same object -- not recompiled

    def test_matches_an_already_loaded_screen_by_declared_name(self, make_user):
        user = make_user("alpha")
        assert user.ensure_screen("Alpha") is user.screen_module

    def test_matches_an_already_loaded_screen_by_module_dunder_name(self, make_user):
        user = make_user("alpha")
        assert user.ensure_screen(user.screen_module.__name__) is user.screen_module

    def test_screens_stay_sorted_by_order_after_adding_one(self, make_user):
        user = make_user("beta")  # order=1
        user.ensure_screen("alpha.py")  # order=0, should sort BEFORE beta
        assert [s.screen.order for s in user.screens] == sorted(s.screen.order for s in user.screens)
