# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/containers.py: Block, ParamBlock, Dialog, Screen -- and,
alongside Block's closable machinery, common.py's delete_unit() helper (its
only caller is Block.close(), so it's covered here rather than in a
standalone file for one small function).

Layout
──────
TestDeleteUnit                  -- delete_unit() in isolation: list/tuple/
                                    nested containers, not-found, first-
                                    match-only semantics.
TestBlockConstruction           -- name/type/value/options wiring.
TestBlockScaler                 -- the three scaler=True branches (empty,
                                    already-nested-list, flat).
TestBlockLLMDependencies        -- the llm=True/list/Unit/dict/invalid
                                    dispatch in Block.__init__.
TestBlockReactivity             -- set_reactivity propagation, and
                                    __setattr__ re-triggering it when
                                    .value is reassigned on a live block.
TestBlockFindParamsCompactView  -- find(), params, compact_view.
TestBlockClosable                -- close(): self._user vs. the
                                    Unishare.context_user() fallback, and
                                    removal from list/tuple/nested screens.
TestParamBlockValueDispatch      -- every case in the params.setter
                                    match/case (Switch/Edit/Range/Select/
                                    Tree/nested ParamBlock/skips/raises).
TestParamBlockRowGrouping        -- row= grouping and the cnt reset after
                                    a nested ParamBlock.
TestParamBlockParamsRoundtrip    -- getter/setter round trip, including
                                    reassigning params on an already-live
                                    (reactive) block.
TestDialog                       -- construction, button wiring,
                                    dialog_command_handler.
TestScreen                       -- construction, set_reactivity fan-out.

Regression tests for the three bugs fixed alongside this suite are labelled
`test_regression_*` (matching tests/units/test_units.py's convention):

  * Block(..., scaler=True) with a flat (non-nested-list) set of elements
    built `self.value[0] = [self.value, scaler]` -- using the *whole*
    `self.value` list instead of just `self.value[0]`. This made
    `self.value[0][0] is self.value`, a circular structure that sent the
    very next line (`for elem in flatten(self.value)`) into unbounded
    recursion -- see TestBlockScaler.
  * delete_unit() mutated its container via `.pop()`, which raises
    AttributeError when that container is a tuple rather than a list.
    screen.blocks legitimately can *be* a tuple (e.g. a screen module
    writing `blocks = block_a, block_b`, as unisi's own
    test_apps/blocks/screens/main.py does) -- see TestDeleteUnit and
    TestBlockClosable.
  * ParamBlock('Block name', name='a data field') raised "got multiple
    values for argument 'name'": the constructor's own `name` parameter
    (the block's name) collided with a same-named entry meant for
    **params, even though **params is specifically meant to accept
    arbitrary caller-chosen field names -- see
    TestParamBlockValueDispatch.test_regression_name_field_via_params.
"""
from types import SimpleNamespace

import pytest

from unisi.common import Unishare, TypeMessage, delete_unit
from unisi.containers import Block, ParamBlock, Dialog, Screen
from unisi.units import Unit, Edit, Button, Switch, Range, Select, Tree, ContentScaler, ChangedProxy
from unisi.tables import Table


# ──────────────────────────────────────────────────────────────────────── #
#  delete_unit (common.py, used only by Block.close)                      #
# ──────────────────────────────────────────────────────────────────────── #

class TestDeleteUnit:
    """delete_unit(units, name) -> (found, updated_units). Nothing is
    mutated in place -- every level is rebuilt so a tuple can be "deleted
    from" just as well as a list (see module docstring for why the old,
    mutate-in-place version couldn't do that)."""

    def test_direct_match_in_list(self):
        a, b = Unit('A'), Unit('B')
        found, updated = delete_unit([a, b], 'B')
        assert found is True
        assert updated == [a]

    def test_original_list_is_not_mutated(self):
        """The old implementation mutated `units` via .pop(); the new one
        rebuilds and returns instead, so the caller's original reference
        is left untouched -- callers must use the returned value."""
        a, b = Unit('A'), Unit('B')
        original = [a, b]
        delete_unit(original, 'B')
        assert original == [a, b]

    def test_not_found_returns_false_and_equivalent_container(self):
        a, b = Unit('A'), Unit('B')
        found, updated = delete_unit([a, b], 'nope')
        assert found is False
        assert updated == [a, b]

    def test_regression_direct_match_in_top_level_tuple(self):
        """Previously crashed: `screen.blocks = block_a, block_b` (a bare
        tuple, exactly what test_apps/blocks/screens/main.py's
        `blocks= [block,bottom_block],config_area` produces) with the
        closable block as a *direct* tuple entry -- delete_unit tried
        `tuple_instance.pop(i)`, which doesn't exist on tuples."""
        a, b = Unit('A'), Unit('B')
        found, updated = delete_unit((a, b), 'B')
        assert found is True
        assert updated == (a,)
        assert isinstance(updated, tuple)

    def test_regression_match_inside_nested_tuple(self):
        b, c = Unit('B'), Unit('C')
        a = Unit('A')
        found, updated = delete_unit([a, (b, c)], 'C')
        assert found is True
        assert updated == [a, (b,)]
        assert isinstance(updated[1], tuple)

    def test_empty_sublist_is_dropped_after_removal(self):
        a, b = Unit('A'), Unit('B')
        found, updated = delete_unit([a, [b]], 'B')
        assert found is True
        assert updated == [a]

    def test_empty_subtuple_is_dropped_after_removal(self):
        a, b = Unit('A'), Unit('B')
        found, updated = delete_unit([a, (b,)], 'B')
        assert found is True
        assert updated == [a]

    def test_only_the_first_match_is_removed(self):
        """Two units happen to share a name (name collisions aren't
        prevented elsewhere) -- only the first, in depth-first left-to-
        right order, is removed."""
        first = Unit('dup')
        second = Unit('dup')
        tail = Unit('tail')
        found, updated = delete_unit([[first], second, tail], 'dup')
        assert found is True
        # first is inside a sublist that's now empty and gets dropped;
        # second (same name, encountered later) is untouched.
        assert updated == [second, tail]

    def test_deeply_nested_match(self):
        target = Unit('deep')
        a, b, c = Unit('a'), Unit('b'), Unit('c')
        tree = [a, [b, [target, c]]]
        found, updated = delete_unit(tree, 'deep')
        assert found is True
        assert updated == [a, [b, [c]]]


# ──────────────────────────────────────────────────────────────────────── #
#  Block construction                                                     #
# ──────────────────────────────────────────────────────────────────────── #

class TestBlockConstruction:
    def test_name_type_and_value(self):
        a, b = Unit('a'), Unit('b')
        block = Block('MyBlock', a, b)
        assert block.name == 'MyBlock'
        assert block.type == 'block'
        assert block.value == [a, b]

    def test_no_elements(self):
        block = Block('Empty')
        assert block.value == []

    def test_options_applied_via_add(self):
        block = Block('B', Unit('x'), icon='star', width=100)
        assert block.icon == 'star'
        assert block.width == 100

    def test_nested_list_layout_preserved(self):
        a, b, c = Unit('a'), Unit('b'), Unit('c')
        block = Block('B', [a, b], c)
        assert block.value == [[a, b], c]

    def test_closable_option_installs_close_method(self):
        block = Block('B', Unit('x'), closable=True)
        assert callable(block.close)

    def test_not_closable_by_default(self):
        block = Block('B', Unit('x'))
        assert not hasattr(block, 'close')


# ──────────────────────────────────────────────────────────────────────── #
#  Block scaler=True wiring                                               #
# ──────────────────────────────────────────────────────────────────────── #

class TestBlockScaler:
    """scaler=True installs a ContentScaler (Range slider) as (part of)
    the block's header widget slot (self.value[0]) and wires its
    `elements` callback to self.scroll_list. ContentScaler's own logic
    (scaling math, kwargs handling) is covered in test_units.py -- these
    tests are about Block wiring it into the right spot."""

    def test_empty_block(self):
        block = Block('B', scaler=True)
        assert isinstance(block.scaler, ContentScaler)
        assert block.value == [[block.scaler]]

    def test_first_element_already_a_list(self):
        a, b, c = Unit('a'), Unit('b'), Unit('c')
        block = Block('B', [a, b], c, scaler=True)
        assert block.value[0] == [a, b, block.scaler]
        assert block.value[1] is c

    def test_regression_flat_elements_no_infinite_recursion(self):
        """Regression: used to build a self-referential list
        (self.value[0][0] is self.value) and crash with RecursionError
        from the flatten() call a few lines later in __init__. See module
        docstring for the exact old/new line."""
        a, b = Unit('a'), Unit('b')
        block = Block('B', a, b, scaler=True)
        assert block.value[0] == [a, block.scaler]
        assert block.value[1] is b
        # No self-reference anywhere in the structure.
        assert block.value[0][0] is not block.value

    def test_scaler_elements_callback_reads_scroll_list(self):
        block = Block('B', Unit('a'), scaler=True)
        block.scroll = False
        assert block.scaler.elements() == []
        img = Unit('img', 'url')
        block.scroll_list = [img]
        assert block.scaler.elements() == [img]


# ──────────────────────────────────────────────────────────────────────── #
#  Block llm dependency wiring                                            #
# ──────────────────────────────────────────────────────────────────────── #

class TestBlockLLMDependencies:
    def test_llm_true_depends_on_every_other_non_command_element(self):
        a, b = Edit('A', 1), Edit('B', 2)
        cmd = Button('Go')
        target = Edit('Target', 0, llm=True)
        Block('blk', a, b, cmd, target)
        assert target.llm is False   # "exactly" is False for llm=True
        assert set(target._llm_dependencies) == {a, b}
        assert cmd not in target._llm_dependencies
        assert target not in target._llm_dependencies

    def test_llm_explicit_list_is_used_exactly(self):
        dep1, dep2, other = Edit('D1', 1), Edit('D2', 2), Edit('O', 3)
        target = Edit('Target', 0, llm=[dep1, dep2])
        Block('blk', dep1, dep2, other, target)
        assert target.llm is True   # "exactly" is True for an explicit list
        assert target._llm_dependencies == [dep1, dep2]

    def test_llm_single_unit_is_wrapped_in_a_list(self):
        dep = Edit('D', 1)
        target = Edit('Target', 0, llm=dep)
        Block('blk', dep, target)
        assert target.llm is True
        assert target._llm_dependencies == [dep]

    def test_llm_dict_on_table_builds_per_field_dependencies(self):
        tbl = Table('T', 0, headers=['x'], rows=[[1]], llm={'x': True})
        Block('blk', tbl)
        assert tbl.llm is True
        assert tbl._llm_dependencies == {'x': True}

    def test_llm_dict_on_non_table_raises(self):
        bad = Edit('Bad', 0, llm={'x': True})
        with pytest.raises(AttributeError, match='dictionary only for tables'):
            Block('blk', bad)

    def test_llm_invalid_value_raises(self):
        bad = Edit('Bad', 0, llm=42)
        with pytest.raises(AttributeError, match='Invalid llm parameter'):
            Block('blk', bad)

    def test_llm_true_with_no_other_elements_becomes_none(self, capsys):
        """No dependency candidates (only itself + a command) -> llm is
        reset to None and a warning is printed, rather than raising."""
        solo = Edit('Solo', 0, llm=True)
        Block('blk', solo, Button('OnlyCommand'))
        assert solo.llm is None
        assert 'Empty dependency list' in capsys.readouterr().out

    def test_llm_dependency_wiring_actually_fires_emit(self):
        """add_changed_handler composes emit() onto the dependency's
        changed callback -- verify the composition really runs emit, not
        just that _llm_dependencies looks right."""
        emitted = []

        class ProbeEdit(Edit):
            async def emit(self, *_):
                emitted.append(self.name)

        dep = Edit('Dep', 'x')
        target = ProbeEdit('Target', 0, llm=True)
        Block('blk', dep, target)

        import asyncio
        asyncio.run(dep.changed(dep, 'new value'))
        assert emitted == ['Target']


# ──────────────────────────────────────────────────────────────────────── #
#  Block reactivity                                                       #
# ──────────────────────────────────────────────────────────────────────── #

class TestBlockReactivity:
    def test_set_reactivity_propagates_to_children(self, fake_user):
        a, b = Unit('a'), Unit('b')
        block = Block('B', a, b)
        block.set_reactivity(fake_user)
        assert block._user is fake_user
        assert a._mark_changed is not None
        assert b._mark_changed is not None

    def test_set_reactivity_with_falsy_user_only_records_it(self, fake_user):
        """set_reactivity(None) (or any falsy user) records _user but
        skips the actual wiring -- matches the `if user:` guard."""
        a = Unit('a')
        block = Block('B', a)
        block.set_reactivity(None)
        assert block._user is None
        assert a._mark_changed is None

    def test_regression_reassigning_value_on_live_block_reactivates_new_children(self, fake_user):
        """__setattr__ re-triggers set_reactivity whenever .value is
        reassigned on an already-live block, so units swapped in later
        (not just the ones present at construction time) still become
        reactive. This also exercises the subtle fact that self.value is
        *already* a ChangedProxy by this point (Unit.set_reactivity wraps
        it), yet flatten() still sees through it correctly."""
        block = Block('B', Unit('orig'))
        block.set_reactivity(fake_user)

        fresh = Unit('fresh', 'v1')
        assert fresh._mark_changed is None
        block.value = [fresh]
        assert fresh._mark_changed is not None

        fresh.value = 'v2'
        assert (fresh, 'value', 'v2') in fake_user.calls

    def test_nested_block_children_become_reactive_too(self, fake_user):
        inner_child = Unit('inner')
        inner = Block('Inner', inner_child)
        outer = Block('Outer', inner)
        outer.set_reactivity(fake_user)
        assert inner._mark_changed is not None
        assert inner_child._mark_changed is not None


# ──────────────────────────────────────────────────────────────────────── #
#  Block.find / .params / .compact_view                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestBlockFindParamsCompactView:
    def test_find_by_object(self):
        a, b = Unit('a'), Unit('b')
        block = Block('B', a, [b])
        assert block.find(a) is a
        assert block.find(b) is b

    def test_find_by_name(self):
        a = Unit('a')
        block = Block('B', a)
        assert block.find('a') is a

    def test_find_missing_returns_none(self):
        block = Block('B', Unit('a'))
        assert block.find('missing') is None

    def test_params_maps_names_to_values(self):
        e1, e2 = Edit('E1', 10), Edit('E2', 'hi')
        block = Block('B', e1, e2)
        assert block.params == {'E1': 10, 'E2': 'hi'}

    def test_compact_view_skips_falsy_values(self):
        zero = Edit('Zero', 0)
        text = Edit('Text', 'hi')
        block = Block('B', zero, text)
        assert block.compact_view == 'Text : hi'

    def test_compact_view_joins_multiple_with_comma(self):
        a, b = Edit('A', 1), Edit('B', 2)
        block = Block('B', a, b)
        assert block.compact_view == 'A : 1,B : 2'


# ──────────────────────────────────────────────────────────────────────── #
#  Block.closable / close()                                               #
# ──────────────────────────────────────────────────────────────────────── #

class TestBlockClosable:
    """close() reads `self._user` if set, else falls back to
    Unishare.context_user() -- exercised via a small local stand-in
    (rather than tests/units/conftest.py's FakeUser, which deliberately
    has no `.screen`) since that's specifically what's under test here."""

    def make_screen_user(self, blocks):
        screen = SimpleNamespace(blocks=blocks, toolbar=[])
        user = SimpleNamespace(screen=screen, register_changed_unit=lambda *a, **k: None)
        return user

    def test_close_removes_itself_from_a_list(self):
        target = Block('closeme', Unit('x'), closable=True)
        keep = Block('keep', Unit('y'))
        user = self.make_screen_user([keep, target])
        target._user = user

        target.close()
        assert user.screen.blocks == [keep]

    def test_regression_close_removes_itself_from_a_top_level_tuple(self):
        """Regression: screen.blocks as a bare tuple (the
        `blocks = block_a, block_b` idiom) with the closable block as a
        direct tuple entry used to crash -- see TestDeleteUnit."""
        target = Block('closeme', Unit('x'), closable=True)
        keep = Block('keep', Unit('y'))
        user = self.make_screen_user((keep, target))
        target._user = user

        target.close()
        assert user.screen.blocks == (keep,)
        assert isinstance(user.screen.blocks, tuple)

    def test_close_falls_back_to_unishare_context_user(self, monkeypatch):
        target = Block('closeme', Unit('x'), closable=True)
        keep = Block('keep', Unit('y'))
        user = self.make_screen_user([keep, target])
        assert target._user is None  # not set -> must use the fallback

        monkeypatch.setattr(Unishare, 'context_user', lambda: user)
        target.close()
        assert user.screen.blocks == [keep]

    def test_close_when_not_found_leaves_blocks_untouched(self):
        target = Block('closeme', Unit('x'), closable=True)
        other = Block('other', Unit('y'))
        user = self.make_screen_user([other])   # target itself isn't in there
        target._user = user

        original = user.screen.blocks
        target.close()
        assert user.screen.blocks is original   # untouched, not even reassigned

    def test_close_on_reactive_screen_unwraps_changed_proxy(self, fake_user):
        """screen.blocks is a ChangedProxy once the screen is reactive
        (Unit.set_reactivity wraps every non-atomic attribute) -- close()
        must unwrap it before handing it to delete_unit, and the
        reassignment must go back through the reactive screen correctly."""
        screen = Screen('S')
        target = Block('closeme', Unit('x'), closable=True)
        keep = Block('keep', Unit('y'))
        screen.blocks = (keep, target)
        screen.toolbar = []
        fake_user.screen = screen
        screen.set_reactivity(fake_user)
        target._user = fake_user

        assert isinstance(screen.blocks, ChangedProxy)
        target.close()

        real = screen.blocks._obj if isinstance(screen.blocks, ChangedProxy) else screen.blocks
        assert real == (keep,)


# ──────────────────────────────────────────────────────────────────────── #
#  ParamBlock: value -> widget dispatch                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestParamBlockValueDispatch:
    def test_bool_becomes_switch(self):
        pb = ParamBlock('P', row=10, flag=True)
        el = pb._name2elem['flag']
        assert isinstance(el, Switch)
        assert el.value is True
        assert el.name == 'Flag'   # pretty4()'d

    def test_str_becomes_edit_string(self):
        pb = ParamBlock('P', row=10, label='hello')
        el = pb._name2elem['label']
        assert isinstance(el, Edit)
        assert el.value == 'hello'
        assert el.type == 'string'

    def test_int_becomes_edit_number(self):
        pb = ParamBlock('P', row=10, age=30)
        el = pb._name2elem['age']
        assert isinstance(el, Edit)
        assert el.value == 30
        assert el.type == 'number'

    def test_float_becomes_edit_number(self):
        pb = ParamBlock('P', row=10, height=1.75)
        el = pb._name2elem['height']
        assert isinstance(el, Edit)
        assert el.value == 1.75

    def test_regression_name_field_via_params(self):
        """Regression: ParamBlock('Block name', name='a value') raised
        'got multiple values for argument name' because `name` (the
        constructor's own first parameter, the block's own name) collided
        with a same-named **params entry -- even though **params is
        specifically meant to accept arbitrary caller-chosen field names.
        Fixed by making the constructor's `name` positional-only."""
        pb = ParamBlock('Settings', row=10, name='Bob', age=30)
        assert pb.name == 'Settings'
        el = pb._name2elem['name']
        assert isinstance(el, Edit)
        assert el.value == 'Bob'

    def test_tuple_with_three_numeric_options_becomes_range(self):
        pb = ParamBlock('P', row=10, volume=(5, [0, 10, 1]))
        el = pb._name2elem['volume']
        assert isinstance(el, Range)
        assert el.value == 5
        assert el.options == [0, 10, 1]

    def test_tuple_with_list_options_becomes_select(self):
        pb = ParamBlock('P', row=10, color=('red', ['red', 'green', 'blue']))
        el = pb._name2elem['color']
        assert isinstance(el, Select)
        assert el.value == 'red'
        assert el.options == ['red', 'green', 'blue']

    def test_tuple_with_dict_options_becomes_tree(self):
        tree_options = {'a': {'b': {}}}
        pb = ParamBlock('P', row=10, path=('a', tree_options))
        el = pb._name2elem['path']
        assert isinstance(el, Tree)
        assert el.options == tree_options

    def test_tuple_wrong_length_is_silently_skipped(self):
        pb = ParamBlock('P', row=10, weird=(1, 2, 3), ok=1)
        assert 'weird' not in pb._name2elem
        assert 'ok' in pb._name2elem

    def test_tuple_with_dict_first_element_is_silently_skipped(self):
        pb = ParamBlock('P', row=10, weird=({'x': 1}, ['a']))
        assert 'weird' not in pb._name2elem

    def test_invalid_options_type_raises_value_error(self):
        with pytest.raises(ValueError, match='has to be a list or tuple'):
            ParamBlock('P', row=10, bad=(1, 'not-a-list-or-dict'))

    def test_dict_with_default_strict_recurses_into_nested_paramblock(self):
        pb = ParamBlock('P', row=10, sub={'inner_a': 1, 'inner_b': 2})
        sub = pb._name2elem['sub']
        assert isinstance(sub, ParamBlock)
        assert sub.params == {'inner_a': 1, 'inner_b': 2}
        assert pb.params == {'sub': {'inner_a': 1, 'inner_b': 2}}

    def test_dict_with_strict_true_raises(self):
        with pytest.raises(ValueError, match='not supported'):
            ParamBlock('P', row=10, strict=True, sub={'a': 1})

    def test_dict_with_strict_false_is_silently_skipped(self):
        pb = ParamBlock('P', row=10, strict=False, sub={'a': 1}, ok=1)
        assert 'sub' not in pb._name2elem
        assert 'ok' in pb._name2elem


# ──────────────────────────────────────────────────────────────────────── #
#  ParamBlock: row grouping                                               #
# ──────────────────────────────────────────────────────────────────────── #

class TestParamBlockRowGrouping:
    def test_groups_of_row_size(self):
        pb = ParamBlock('P', row=2, a=1, b=2, c=3, d=4, e=5)
        # value[0] is the reserved (empty here) slot for positional args;
        # the rest are generated rows of at most `row` elements each.
        rows = pb.value[1:]
        assert [len(row) for row in rows] == [2, 2, 1]
        assert [u.name for u in rows[0]] == ['A', 'B']
        assert [u.name for u in rows[1]] == ['C', 'D']
        assert [u.name for u in rows[2]] == ['E']

    def test_row_of_one_puts_every_field_on_its_own_row(self):
        pb = ParamBlock('P', row=1, a=1, b=2)
        rows = pb.value[1:]
        assert [len(row) for row in rows] == [1, 1]

    def test_cnt_resets_after_a_nested_paramblock(self):
        """A nested ParamBlock (dict + strict='recurse') is appended as
        its own value entry (not folded into a row of plain widgets), and
        resets the row counter so the *next* plain field starts a fresh
        row rather than continuing to fill whatever row was in progress."""
        pb = ParamBlock('P', row=2, a=1, sub={'x': 1}, b=2, c=3)
        rows = pb.value[1:]
        assert [u.name for u in rows[0]] == ['A']
        assert isinstance(rows[1], ParamBlock)
        assert [u.name for u in rows[2]] == ['B', 'C']

    def test_positional_args_occupy_the_first_value_slot(self):
        header = Button('Custom header')
        pb = ParamBlock('P', [header], row=10, a=1)
        assert pb.value[0] == [header]
        assert [u.name for u in pb.value[1]] == ['A']


# ──────────────────────────────────────────────────────────────────────── #
#  ParamBlock: params getter/setter round trip                            #
# ──────────────────────────────────────────────────────────────────────── #

class TestParamBlockParamsRoundtrip:
    def test_getter_reflects_generated_elements(self):
        pb = ParamBlock('P', row=10, x=1, y='hi')
        assert pb.params == {'x': 1, 'y': 'hi'}

    def test_setter_rebuilds_value_and_name2elem(self):
        pb = ParamBlock('P', row=10, x=1, y='hi')
        pb.params = {'z': True, 'w': 3.5}
        assert pb.params == {'z': True, 'w': 3.5}
        assert 'x' not in pb._name2elem
        assert 'y' not in pb._name2elem

    def test_regression_reassigning_params_on_a_live_block_reactivates_new_children(self, fake_user):
        """Regression-guarding test for the tricky bit documented right
        above params.setter's tail in containers.py: self.value is
        already a ChangedProxy by the time this setter runs on a live
        block (Unit.set_reactivity wrapped it during the *first*
        set_reactivity call), and flatten() has to still correctly walk
        through it (via ChangedProxy's isinstance/__class__ passthrough)
        to reach the freshly (re)built children and make them reactive
        too -- not just leave them inert."""
        pb = ParamBlock('P', row=10, x=1)
        pb.set_reactivity(fake_user)

        pb.params = {'y': 'new'}
        new_el = pb._name2elem['y']
        assert new_el._mark_changed is not None

        new_el.value = 'changed'
        assert (new_el, 'value', 'changed') in fake_user.calls

    def test_reassigning_params_on_a_live_block_fills_parents_when_tracked(self, fake_user):
        pb = ParamBlock('P', row=10, x=1)
        screen = SimpleNamespace(_parents={})
        fake_user.screen = screen
        pb.set_reactivity(fake_user)

        pb.params = {'y': 'new'}
        new_el = pb._name2elem['y']
        assert screen._parents.get(new_el) is pb

    def test_reassigning_params_on_a_non_live_block_does_not_touch_reactivity(self):
        pb = ParamBlock('P', row=10, x=1)
        pb.params = {'y': 'new'}   # no set_reactivity call at all -- must not raise
        assert pb._name2elem['y']._mark_changed is None


# ──────────────────────────────────────────────────────────────────────── #
#  Dialog                                                                  #
# ──────────────────────────────────────────────────────────────────────── #

class TestDialog:
    def test_default_commands_are_ok_and_cancel(self):
        async def cb(dlg, cmd):
            pass

        dlg = Dialog('Are you sure?', cb)
        assert dlg.type == 'dialog'
        assert dlg.name == 'Are you sure?'
        buttons = dlg.value
        assert [b.name for b in buttons] == ['Ok', 'Cancel']

    def test_first_button_is_primary_and_spaced(self):
        async def cb(dlg, cmd):
            pass

        dlg = Dialog('Q', cb)
        ok_button, cancel_button = dlg.value
        assert ok_button.color == 'primary'
        assert ok_button.space is True
        assert cancel_button.color == 'secondary'

    def test_buttons_are_wired_to_dialog_command_handler(self):
        async def cb(dlg, cmd):
            pass

        dlg = Dialog('Q', cb)
        for button in dlg.value:
            assert button.changed == dlg.dialog_command_handler
            assert button.close is True

    def test_custom_commands(self):
        async def cb(dlg, cmd):
            pass

        dlg = Dialog('Q', cb, commands=['Yes', 'No', 'Maybe'])
        assert [b.name for b in dlg.value] == ['Yes', 'No', 'Maybe']

    def test_content_is_wrapped_between_empty_header_slot_and_buttons(self):
        async def cb(dlg, cmd):
            pass

        extra = Edit('extra', 'x')
        dlg = Dialog('Q', cb, extra)
        assert dlg.value[0] == []
        assert dlg.value[1] is extra
        assert [b.name for b in dlg.value[2]] == ['Ok', 'Cancel']

    def test_no_content_means_value_is_just_the_buttons(self):
        async def cb(dlg, cmd):
            pass

        dlg = Dialog('Q', cb)
        assert all(isinstance(v, Button) for v in dlg.value)

    @pytest.mark.asyncio
    async def test_dialog_command_handler_happy_path(self, monkeypatch):
        calls = []

        async def cb(dlg, cmd):
            calls.append((dlg.name, cmd))
            return 'cb-result'

        dlg = Dialog('Proceed?', cb)

        sent = []

        class FakeUser:
            active_dialog = 'was-open'

            async def send(self, message, persist=True):
                sent.append((message, persist))

        user = FakeUser()
        monkeypatch.setattr(Unishare, 'context_user', lambda: user)

        result = await dlg.dialog_command_handler(dlg.value[0], 'ignored')

        assert user.active_dialog is None
        assert len(sent) == 1
        message, persist = sent[0]
        assert message.type == 'action'
        assert message.value == 'close'
        assert persist is False
        assert calls == [('Proceed?', 'Ok')]
        assert result == 'cb-result'

    @pytest.mark.asyncio
    async def test_dialog_command_handler_without_active_user_is_a_noop(self, monkeypatch):
        calls = []

        async def cb(dlg, cmd):
            calls.append(cmd)

        dlg = Dialog('Q', cb)
        monkeypatch.setattr(Unishare, 'context_user', lambda: None)

        result = await dlg.dialog_command_handler(dlg.value[1], 'x')
        assert result is None
        assert calls == []


# ──────────────────────────────────────────────────────────────────────── #
#  Screen                                                                  #
# ──────────────────────────────────────────────────────────────────────── #

class TestScreen:
    def test_construction(self):
        screen = Screen('Main')
        assert screen.name == 'Main'
        assert screen.type == 'screen'
        assert not hasattr(screen, 'value')

    def test_set_reactivity_propagates_to_blocks_and_toolbar(self, fake_user):
        block = Block('B', Unit('x'))
        tool = Unit('tool')
        screen = Screen('Main')
        screen.blocks = [block]
        screen.toolbar = [tool]

        screen.set_reactivity(fake_user)

        assert block._mark_changed is not None
        assert tool._mark_changed is not None

    def test_set_reactivity_handles_nested_and_tuple_blocks(self, fake_user):
        inner = Block('Inner', Unit('x'))
        top = Block('Top', Unit('y'))
        screen = Screen('Main')
        screen.blocks = ([inner], top)   # mixed tuple/list nesting
        screen.toolbar = []

        screen.set_reactivity(fake_user)

        assert inner._mark_changed is not None
        assert top._mark_changed is not None
