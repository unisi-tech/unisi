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
import ast
import random
import sys

import pytest

from unisi.modules import ScreenInfo, declared_in, declared_screen_info, screen_info_from_module
from unisi.units import Button, Edit


def record_parsed(monkeypatch):
    """the sizes of what ast.parse is asked to parse from now on"""
    sizes, parse = [], ast.parse

    def parsing(source, *args, **kwargs):
        sizes.append(len(source))
        return parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", parsing)
    return sizes


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

    def test_entries_carry_what_the_files_declare(self):
        """
        build_screen_registry() doesn't compile anything, but it reads the `name`, `icon` and
        `order` a screen file declares at its module level as plain literals (ast, the file is not
        run). So the menu has the names of the screens not loaded yet, they are found by their names,
        and the registry is in the order of the screens from the start.
        """
        from unisi.users import User
        registry = User.build_screen_registry()
        assert [(info.name, info.file, info.order) for info in registry] == [
            ("Alpha", "alpha.py", 0), ("Beta", "beta.py", 1), ("UsesBlock", "uses_block.py", 2)]

    def test_what_is_not_declared_as_a_literal_is_left_to_the_load(self, real_screens_dir_modules):
        """a computed or odd-typed declaration is no use before the screen runs: the entry stays
        a placeholder (blank name, order 0) for it, and the load fills it in"""
        from unisi.users import User
        (real_screens_dir_modules / "plain.py").write_text('name = "Plain"\nicon = "home"\norder = 5\n')
        (real_screens_dir_modules / "computed.py").write_text('name = "Comp" + "uted"\norder = 2 + 1\n')
        (real_screens_dir_modules / "typed.py").write_text('name = 7\norder = True\nicon = ["x"]\n')
        (real_screens_dir_modules / "broken.py").write_text('def (\nname = "Broken"\n')
        registry = {info.file: info for info in User.build_screen_registry()}

        assert (registry["plain.py"].name, registry["plain.py"].icon, registry["plain.py"].order) == ("Plain", "home", 5)
        for file in ("computed.py", "typed.py", "broken.py"):
            assert (registry[file].name, registry[file].icon, registry[file].order) == ("", None, 0)

    def test_declared_screen_info_takes_the_last_literal_and_forgets_a_computed_one(self, real_screens_dir_modules):
        (real_screens_dir_modules / "again.py").write_text('name = "First"\nname = "Second"\norder: int = 4\n')
        (real_screens_dir_modules / "later.py").write_text('name = "Literal"\nname = make_name()\norder = 1\n')
        (real_screens_dir_modules / "nested.py").write_text('def prepare():\n    name = "Local"\n    order = 9\n')
        assert declared_screen_info("again.py") == {"name": "Second", "order": 4}
        assert declared_screen_info("later.py") == {"order": 1}
        assert declared_screen_info("nested.py") == {}          # a local of a function is not the screen's
        assert declared_screen_info("missing.py") == {}

    def test_only_the_head_of_a_screen_is_parsed(self, real_screens_dir_modules, monkeypatch):
        """Every screen file is asked at the first session of the server: reading three lines at
        the top of a long screen must not cost the parsing of all of it"""
        head = 'from unisi import *\nname = "Long"\norder = 3\n'
        (real_screens_dir_modules / "long.py").write_text(head + "def handler(value):\n    return value\n" * 5000)
        parsed = record_parsed(monkeypatch)
        assert declared_screen_info("long.py") == {"name": "Long", "order": 3}
        assert parsed == [len(head)]

    def test_a_screen_declaring_nothing_is_not_parsed_at_all(self, real_screens_dir_modules, monkeypatch):
        """a name that only starts with name, icon or order is not a declaration"""
        (real_screens_dir_modules / "bare.py").write_text(
            "from unisi import *\nnames = ['a']\norder_by = 1\niconic = None\nblocks = []\n")
        parsed = record_parsed(monkeypatch)
        assert declared_screen_info("bare.py") == {}
        assert parsed == []

    def test_a_declaration_far_below_the_top_is_found(self, real_screens_dir_modules):
        body = "def handler(value):\n    return value\n" * 300
        (real_screens_dir_modules / "late.py").write_text('name = "Late"\n' + body + "order = 7\n" + body)
        assert declared_screen_info("late.py") == {"name": "Late", "order": 7}

    def test_a_declaration_that_goes_on_in_the_next_lines_is_read_from_the_whole_file(
            self, real_screens_dir_modules, monkeypatch):
        (real_screens_dir_modules / "wrapped.py").write_text(
            'name = (\n    "Wrap"\n    "ped"\n)\norder = (\n    4\n)\nblocks = []\n')
        parsed = record_parsed(monkeypatch)
        assert declared_screen_info("wrapped.py") == {"name": "Wrapped", "order": 4}
        assert len(parsed) == 2         # the head ended inside the statement, so all of the file decided

    def test_what_looks_like_a_declaration_inside_a_string_is_not_one(self, real_screens_dir_modules):
        (real_screens_dir_modules / "doc_first.py").write_text(
            '"""A screen.\nname = \'Fake\'\norder = 9\n"""\nname = "Real"\n')
        (real_screens_dir_modules / "doc_last.py").write_text(
            'name = "Real"\nNOTES = """\nname = \'Fake\'\norder = 9\n"""\n')
        assert declared_screen_info("doc_first.py") == {"name": "Real"}
        assert declared_screen_info("doc_last.py") == {"name": "Real"}

    def test_a_file_with_a_byte_order_mark_and_windows_line_ends(self, real_screens_dir_modules):
        (real_screens_dir_modules / "windows.py").write_bytes(
            b'\xef\xbb\xbfname = "Win"\r\nblocks = []\r\norder = 2\r\n')
        (real_screens_dir_modules / "first.py").write_bytes(b'\xef\xbb\xbfname = "First"\r\nblocks = []\r\n')
        assert declared_screen_info("windows.py") == {"name": "Win", "order": 2}
        assert declared_screen_info("first.py") == {"name": "First"}    # the mark does not hide the first line

    def test_the_last_line_needs_no_line_end(self, real_screens_dir_modules):
        (real_screens_dir_modules / "eof.py").write_text('blocks = []\nname = "Eof"')
        assert declared_screen_info("eof.py") == {"name": "Eof"}

    def test_a_file_broken_below_its_declarations_still_declares_them(self, real_screens_dir_modules):
        """so the menu names the screen while it is being fixed: the load reports the error"""
        (real_screens_dir_modules / "below.py").write_text('name = "Below"\norder = 1\ndef (\n')
        (real_screens_dir_modules / "above.py").write_text('def (\nname = "Above"\n')
        assert declared_screen_info("below.py") == {"name": "Below", "order": 1}
        assert declared_screen_info("above.py") == {}

    def test_a_declaration_is_a_statement_that_starts_a_line_with_the_name(self, real_screens_dir_modules):
        """the price of not parsing the whole file: `x = name = 'A'`, a declaration behind a
        semicolon or in parentheses is known when the screen is loaded, like a computed one --
        wherever it is in the file"""
        files = {
            "chained.py": 'x = name = "Chained"\norder = 1\n',
            "chained_late.py": 'order = 1\nx = name = "Chained"\n',
            "semicolon.py": 'blocks = []; name = "Semi"\norder = 1\n',
            "semicolon_late.py": 'order = 1\nblocks = []; name = "Semi"\n',
            "paren.py": '(name) = "Paren"\norder = 1\n',
            "paren_late.py": 'order = 1\n(name) = "Paren"\n',
        }
        for file, text in files.items():
            (real_screens_dir_modules / file).write_text(text)
            assert declared_screen_info(file) == {"order": 1}, file
        (real_screens_dir_modules / "both.py").write_text('name = icon = "Both"\n')      # the first one counts
        assert declared_screen_info("both.py") == {"name": "Both"}

    def test_the_head_says_what_the_whole_file_says(self, real_screens_dir_modules):
        """whatever a screen is made of, reading the head of it gives what parsing all of it does"""
        fragments = [
            "from unisi import *\n", "import os\n",
            '"""A screen.\nname = \'Fake\'\norder = 9\n"""\n', "NOTES = '''\nicon = 'fake'\n'''\n",
            'name = "A"\n', "name = 'B'  # a comment\n", "name = 7\n", "name = make_name()\n",
            'name = (\n    "Wr"\n    "apped"\n)\n',
            "icon = 'home'\n", "icon = None\n", "icon: str = 'tune'\n",
            "order = 3\n", "order = 2.5\n", "order = True\n", "order = 1 + 1\n", "order = (\n    4\n)\n",
            "def handler(name='x'):\n    order = 9\n    return name\n",
            "class K:\n    name = 'inner'\n    order = 8\n",
            "if True:\n    name = 'conditional'\n",
            "names = []\norder_by = 1\n",
            "x = name = 'chained'\n", "blocks = []; order = 5\n", "(icon) = 'paren'\n", "name = icon = 'both'\n",
            "blocks = [Block('B', Edit('E', ''))]\n",
            "filler = 1\n" * 40,
        ]
        rng = random.Random(7)
        for _ in range(300):
            source = "".join(rng.choice(fragments) for _ in range(rng.randint(1, 12)))
            (real_screens_dir_modules / "case.py").write_text(source)
            assert declared_screen_info("case.py") == declared_in(ast.parse(source)), source

    def test_the_first_entry_is_the_screen_of_the_lowest_order(self, real_screens_dir_modules):
        """it is the screen a new session starts at: whatever the order of the files"""
        from unisi.users import User
        (real_screens_dir_modules / "a_settings.py").write_text('name = "Settings"\norder = 2\n')
        (real_screens_dir_modules / "b_main.py").write_text('name = "Main"\norder = 1\n')
        (real_screens_dir_modules / "c_help.py").write_text('name = "Help"\n')
        assert [info.name for info in User.build_screen_registry()] == ["Help", "Main", "Settings"]

    def test_screens_of_the_same_order_are_listed_by_their_file_names(self, real_screens_dir_modules):
        """not in the order the file system happens to list them, which differs from system to
        system: the first entry is the screen a new session starts at"""
        from unisi.users import User
        for file in ("c.py", "a.py", "b.py"):
            (real_screens_dir_modules / file).write_text("")
        assert [info.file for info in User.build_screen_registry()] == ["a.py", "b.py", "c.py"]

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

    def test_found_by_declared_name_before_any_load(self, registry_snapshot):
        """
        The entries of build_screen_registry() carry the names the files declare (see
        TestBuildScreenRegistry), so a screen that no user has loaded yet in this process is
        found by its declared name as well as by its filename.
        """
        from unisi.users import User
        User._screen_registry_ready = False
        User.screen_registry = User.build_screen_registry()  # fresh, nothing loaded yet
        user = object.__new__(User)

        assert user._screen_info("Alpha").file == "alpha.py"  # declared name, not yet loaded by anyone
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

    def test_a_loaded_screen_keeps_its_place_among_the_ones_of_the_same_order(self, real_screens_dir_modules):
        """Screens without an `order` all have the same, 0. The one just loaded used to be moved
        behind the others (removed, appended, then sorted -- and a sort is stable), so the next
        session started at another screen than the one before it."""
        from unisi.users import User
        for name in "abc":
            (real_screens_dir_modules / f"{name}.py").write_text(f'name = "{name.upper()}"\n')
        User.screen_registry = User.build_screen_registry()
        user = object.__new__(User)

        user._upsert_screen_info(ScreenInfo(name="A", file="a.py"))
        assert [info.file for info in user.screen_registry] == ["a.py", "b.py", "c.py"]
        user._upsert_screen_info(ScreenInfo(name="B", file="b.py"))
        assert [info.file for info in user.screen_registry] == ["a.py", "b.py", "c.py"]

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

    def test_block_sees_its_user_while_it_is_imported(self, make_user):
        import builtins
        user = make_user()
        user.load_screen("uses_block.py")
        assert user.modules["blocks.widget"].importing_user is user and not hasattr(builtins, "user")

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

    def test_every_new_session_starts_at_the_same_screen(self, real_screens_dir_modules):
        """With no `order` anywhere the first screen of the registry is the first by name. Each of
        the first few sessions used to start at another one: the one loaded was moved to the end of
        the registry, and the next session took the screen then standing first."""
        from unisi.users import User
        for name in "abc":
            (real_screens_dir_modules / f"{name}.py").write_text(
                f'from unisi import *\nname = "{name.upper()}"\nblocks = [Block("Root", Edit("Field", "x"))]\n')
        users = [User(f"scratch-load-lazy-same-{i}") for i in range(5)]
        assert [user.screen.name for user in users] == ["A"] * 5

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
