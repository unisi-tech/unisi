# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/units.py: ChangedProxy, Unit, smart_complete, and the
whole family of Unit subclasses (Edit, Text, Range, ContentScaler, Button,
Image, Video, Sound, Chart, Switch, Select, Tree, TextArea, HTML).

Layout
──────
TestChangedProxy*      -- the reactivity proxy in isolation (no Unit needed
                           beyond a throwaway one to own `_mark_changed`).
TestUnit*               -- Unit's own machinery: construction, reactivity,
                           mutate/accept/delattr, equality, emit(), etc.
TestSmartComplete        -- the smart_complete() autocomplete factory.
Test<Subclass>           -- one class per Unit subclass, covering defaults,
                           explicit overrides, and any subclass-specific
                           logic (e.g. Range's auto-options, Image's URL
                           masking).

Regression tests for the four bugs fixed alongside this suite are folded
into the relevant class rather than collected separately, and are labelled
`test_regression_*` (matching tests/db_units/test_db.py's convention) so
they're easy to find later:

  * ChangedProxy.__getattribute__ used to hand a *nested proxy* (rather than
    the real Unit) as the `_unit` reference when wrapping a non-atomic
    attribute reached through dotted attribute access (as opposed to
    __getitem__/__iter__, which already threaded the real unit through
    correctly) -- see TestChangedProxyNestedAttributes.
  * Image('a') (or any single-character name/url) raised IndexError from an
    unguarded self.url[1] -- see TestImage.
  * Sound(...) with no explicit `value=` shared ONE mutable {} dict across
    every instance (classic mutable-default-argument bug) -- see TestSound.
  * ContentScaler(name, value) re-passed `name` through *args a second time
    into Range.__init__, corrupting `self.value`/`self.changed` for any
    positional-args call -- see TestContentScaler.
"""
import pytest

from unisi.units import (
    ChangedProxy, Unit, smart_complete,
    Edit, Text, Range, ContentScaler, Button, CameraButton, UploadButton,
    Image, Video, Sound, Chart, Switch, Select, Tree, TextArea, HTML,
)


# ──────────────────────────────────────────────────────────────────────── #
#  ChangedProxy                                                            #
# ──────────────────────────────────────────────────────────────────────── #

class TestChangedProxyListOperations:
    """A ChangedProxy wrapping a list: mutating methods must both perform
    the real mutation on the underlying list AND mark the owning unit
    changed; reads must not.
    """

    def make(self):
        unit = Unit('u')
        proxy = ChangedProxy([1, 2, 3], unit)
        return unit, proxy

    def test_append_mutates_and_marks_changed(self, fake_user):
        unit, proxy = self.make()
        unit.set_reactivity(fake_user)
        proxy.append(4)
        assert proxy._obj == [1, 2, 3, 4]
        assert fake_user.calls  # _mark_changed() was invoked

    @pytest.mark.parametrize('method,args,expected', [
        ('extend', ([4, 5],), [1, 2, 3, 4, 5]),
        ('insert', (0, 9), [9, 1, 2, 3]),
        ('remove', (2,), [1, 3]),
        ('reverse', (), [3, 2, 1]),
        ('sort', (), [1, 2, 3]),
        ('clear', (), []),
    ])
    def test_modifying_methods_mutate_underlying_list(self, fake_user, method, args, expected):
        # Every name in MODIFYING_METHODS marks the unit changed as soon as
        # it's accessed (see __getattribute__), so the owning unit has to
        # be reactive first, exactly like real usage (a ChangedProxy is
        # only ever handed out after set_reactivity wraps the attribute).
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        proxy = ChangedProxy([1, 2, 3], unit)
        getattr(proxy, method)(*args)
        assert proxy._obj == expected
        assert fake_user.calls

    def test_pop_returns_value_and_mutates(self, fake_user):
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        proxy = ChangedProxy([1, 2, 3], unit)
        assert proxy.pop() == 3
        assert proxy._obj == [1, 2]

    def test_reading_length_does_not_mark_changed(self, fake_user):
        unit = Unit('u')
        unit.value = [1, 2, 3]
        unit.set_reactivity(fake_user)
        len(unit.value)
        assert fake_user.calls == []

    def test_iadd_extends_list_and_returns_self(self, fake_user):
        # `proxy += other` calls type(proxy).__iadd__ directly (Python
        # resolves augmented-assignment dunders on the type, bypassing
        # __getattribute__) -- unlike `proxy.__iadd__(other)`, which *would*
        # go through __getattribute__ and never reach ChangedProxy's own
        # __iadd__ at all (list already has its own __iadd__, so it would
        # just be returned and called instead, silently working around the
        # proxy). The operator form is what real `unit.value += [...]` code
        # actually uses, so that's what this test exercises.
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        proxy = ChangedProxy([1, 2], unit)
        proxy += [3, 4]
        assert isinstance(proxy, ChangedProxy)
        assert proxy._obj == [1, 2, 3, 4]
        assert fake_user.calls

    def test_iadd_on_non_list_raises_type_error(self):
        unit = Unit('u')
        proxy = ChangedProxy({'a': 1}, unit)
        with pytest.raises(TypeError):
            proxy += {'b': 2}


class TestChangedProxyDictOperations:
    def test_setitem_mutates_and_marks_changed(self, fake_user):
        unit = Unit('u')
        unit.value = {'a': 1}
        unit.set_reactivity(fake_user)
        unit.value['b'] = 2
        assert unit.value['a'] == 1 and unit.value['b'] == 2
        assert fake_user.calls

    def test_delitem_mutates_and_marks_changed(self, fake_user):
        unit = Unit('u')
        unit.value = {'a': 1, 'b': 2}
        unit.set_reactivity(fake_user)
        del unit.value['a']
        assert dict(unit.value._obj) == {'b': 2}
        assert fake_user.calls

    def test_update_and_setdefault_and_popitem_are_modifying(self, fake_user):
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        proxy = ChangedProxy({'a': 1}, unit)
        proxy.update({'b': 2})
        assert proxy._obj == {'a': 1, 'b': 2}
        proxy.setdefault('c', 3)
        assert proxy._obj['c'] == 3
        key, value = proxy.popitem()
        assert (key, value) == ('c', 3)
        assert fake_user.calls


class TestChangedProxyNestedAccess:
    """__getitem__ / __iter__ re-wrap nested non-atomic values in a fresh
    ChangedProxy so that further mutation keeps propagating `_mark_changed`
    correctly, however deep the structure goes.
    """

    def test_getitem_on_nested_list_returns_proxy(self):
        unit = Unit('u')
        proxy = ChangedProxy([[1, 2], [3, 4]], unit)
        inner = proxy[0]
        assert isinstance(inner, ChangedProxy)
        assert inner._obj == [1, 2]

    def test_getitem_on_atomic_value_returns_raw_value(self):
        unit = Unit('u')
        proxy = ChangedProxy([1, 'two', None], unit)
        assert proxy[0] == 1 and proxy[1] == 'two' and proxy[2] is None
        assert not isinstance(proxy[0], ChangedProxy)

    def test_mutating_nested_list_item_marks_the_real_unit_changed(self, fake_user):
        unit = Unit('u')
        unit.value = [[1, 2], [3, 4]]
        unit.set_reactivity(fake_user)
        unit.value[0].append(99)
        assert unit.value[0]._obj == [1, 2, 99]
        assert fake_user.calls

    def test_deeply_nested_dict_of_lists_mutation_marks_changed(self, fake_user):
        unit = Unit('u')
        unit.value = {'rows': [{'cells': [1, 2]}]}
        unit.set_reactivity(fake_user)
        unit.value['rows'][0]['cells'].append(3)
        assert unit.value['rows'][0]['cells']._obj == [1, 2, 3]
        assert fake_user.calls

    def test_iter_wraps_nested_items_only(self):
        unit = Unit('u')
        proxy = ChangedProxy([[1], 'atomic', 2, None], unit)
        items = list(proxy)
        assert isinstance(items[0], ChangedProxy)
        assert items[1:] == ['atomic', 2, None]


class TestChangedProxyNestedAttributes:
    """Regression coverage for the __getattribute__ nested-wrap bug: a
    ChangedProxy created while walking plain (non-list/dict) attribute
    chains has to thread the *real* Unit through as `_unit`, exactly like
    __getitem__/__iter__ already did, or `_mark_changed()` ends up looking
    for a `_mark_changed` attribute on a plain user object two note further
    down and raises AttributeError.
    """

    class Leaf:
        def __init__(self):
            self.value = 1

    class Branch:
        def __init__(self):
            self.leaf = TestChangedProxyNestedAttributes.Leaf()

    def test_single_level_attribute_set_marks_changed(self, fake_user):
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        unit.branch = self.Branch()
        fake_user.calls.clear()
        unit.branch.leaf = self.Leaf()
        assert fake_user.calls

    def test_regression_two_level_nested_attribute_set_does_not_raise(self, fake_user):
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        unit.branch = self.Branch()
        fake_user.calls.clear()
        # unit.branch is a level-1 proxy (real Unit as _unit -- always
        # worked). unit.branch.leaf is a level-2 proxy: this is exactly the
        # case that used to carry the *parent proxy* as _unit instead of
        # the real unit, so mutating through it raised AttributeError.
        unit.branch.leaf.value = 42
        assert unit.branch.leaf.value == 42
        assert fake_user.calls

    def test_regression_three_level_nested_method_call_does_not_raise(self, fake_user):
        class Deep:
            def __init__(self):
                self.items = [1, 2]

        class Mid:
            def __init__(self):
                self.deep = Deep()

        unit = Unit('u')
        unit.set_reactivity(fake_user)
        unit.mid = Mid()
        fake_user.calls.clear()
        unit.mid.deep.items.append(3)
        assert unit.mid.deep.items._obj == [1, 2, 3]
        assert fake_user.calls

    def test_nested_proxy_unit_reference_is_the_real_unit(self, fake_user):
        unit = Unit('u')
        unit.set_reactivity(fake_user)
        unit.branch = self.Branch()
        nested = unit.branch.leaf
        assert isinstance(nested, ChangedProxy)
        assert nested._unit is unit

    def test_attribute_that_is_already_a_changed_proxy_is_unwrapped_not_double_wrapped(self, fake_user):
        """__getattribute__ unwraps a value that's already a ChangedProxy
        (`value = value._obj`) before deciding whether to re-wrap it --
        guards against double-wrapping when the underlying object already
        stores a proxy as one of its own attributes."""
        class Holder:
            pass

        owner_unit = Unit('owner')
        holder = Holder()
        holder.already_wrapped = ChangedProxy([1, 2], owner_unit)

        outer_unit = Unit('outer')
        outer_unit.set_reactivity(fake_user)
        outer_unit.holder = holder

        result = outer_unit.holder.already_wrapped
        assert isinstance(result, ChangedProxy)
        assert result._obj == [1, 2]
        # re-wrapped fresh (with the real, current unit), not the original
        # proxy object carried over as-is
        assert result._unit is outer_unit


class TestChangedProxyMisc:
    def test_eq_compares_underlying_objects(self):
        unit = Unit('u')
        p1 = ChangedProxy([1, 2], unit)
        p2 = ChangedProxy([1, 2], unit)
        assert p1 == p2
        assert p1 == [1, 2]

    def test_hash_delegates_to_underlying_object(self):
        unit = Unit('u')
        proxy = ChangedProxy('abc', unit)
        assert hash(proxy) == hash('abc')

    def test_len_on_sequence(self):
        unit = Unit('u')
        proxy = ChangedProxy([1, 2, 3], unit)
        assert len(proxy) == 3

    def test_len_falls_back_to_zero_for_non_sized_object(self):
        class NoLen:
            pass
        unit = Unit('u')
        proxy = ChangedProxy(NoLen(), unit)
        assert len(proxy) == 0

    def test_getstate_returns_raw_underlying_object(self):
        unit = Unit('u')
        raw = [1, 2, 3]
        proxy = ChangedProxy(raw, unit)
        assert proxy.__getstate__() is raw


# ──────────────────────────────────────────────────────────────────────── #
#  Unit                                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestUnitConstruction:
    def test_name_only(self):
        u = Unit('widget')
        assert u.name == 'widget'
        assert not hasattr(u, 'value')

    def test_positional_value(self):
        u = Unit('widget', 42)
        assert u.value == 42

    def test_positional_value_and_changed(self):
        def handler(obj, val):
            return val
        u = Unit('widget', 42, handler)
        assert u.value == 42
        assert u.changed is handler

    def test_kwargs_set_arbitrary_attributes(self):
        u = Unit('widget', color='red', width=10)
        assert u.color == 'red'
        assert u.width == 10

    def test_module_level_line_singleton(self):
        from unisi.units import Line
        assert Line.name == '__Line__'
        assert Line.type == 'line'


class TestUnitReactivity:
    def test_set_reactivity_wraps_non_atomic_attributes(self, fake_user):
        u = Unit('widget', value=[1, 2, 3])
        u.set_reactivity(fake_user)
        assert isinstance(u.value, ChangedProxy)

    def test_set_reactivity_does_not_wrap_atomics(self, fake_user):
        u = Unit('widget', value=5, flag=True, label='hi', empty=None)
        u.set_reactivity(fake_user)
        assert u.value == 5 and not isinstance(u.value, ChangedProxy)
        assert u.flag is True
        assert u.label == 'hi'
        assert u.empty is None

    def test_set_reactivity_does_not_wrap_nested_unit(self, fake_user):
        child = Unit('child')
        parent = Unit('parent', value=child)
        parent.set_reactivity(fake_user)
        assert parent.value is child

    def test_set_reactivity_skips_units_with_id(self, fake_user):
        """Persistent units (hasattr 'id') opt out of ChangedProxy wrapping
        entirely -- they track changes through the DB layer instead."""
        u = Unit('widget', value=[1, 2, 3])
        u.id = 'persisted'
        u.set_reactivity(fake_user)
        assert not isinstance(u.value, ChangedProxy)

    def test_set_reactivity_is_idempotent_without_override(self, fake_user):
        u = Unit('widget', value=[1, 2, 3])
        u.set_reactivity(fake_user)
        wrapped_once = u.value
        u.set_reactivity(fake_user)
        assert u.value is wrapped_once

    def test_set_reactivity_override_rewires_to_new_user(self, fake_user):
        u = Unit('widget', value=5)
        u.set_reactivity(fake_user)
        other_user = type(fake_user)()
        u.set_reactivity(other_user, override=True)
        u.color = 'blue'
        assert other_user.calls
        assert fake_user.calls == []

    def test_setattr_after_reactivity_registers_change(self, fake_user):
        u = Unit('widget')
        u.set_reactivity(fake_user)
        u.value = 10
        assert fake_user.calls == [(u, 'value', 10)]

    def test_setattr_before_reactivity_does_not_register(self, fake_user):
        u = Unit('widget')
        u.value = 10
        assert fake_user.calls == []

    def test_setattr_with_underscore_name_never_registers(self, fake_user):
        u = Unit('widget')
        u.set_reactivity(fake_user)
        u._private = 'internal'
        assert fake_user.calls == []

    def test_specific_changed_register_default_contract(self):
        u = Unit('widget')
        assert u.specific_changed_register(None, None) is True
        assert u.specific_changed_register('value', 1) is True
        assert u.specific_changed_register('_private', 1) is False


class TestUnitMutateAcceptDelattr:
    def test_mutate_copies_all_public_attributes(self, fake_user):
        source = Unit('source', color='red', width=5)
        target = Unit('target')
        target.set_reactivity(fake_user)
        target.mutate(source)
        assert target.name == 'source'
        assert target.color == 'red'
        assert target.width == 5

    def test_mutate_marks_changed_once_more_at_the_end(self, fake_user):
        source = Unit('source', color='red')
        target = Unit('target')
        target.set_reactivity(fake_user)
        target.mutate(source)
        # one call per copied public attribute, plus a final bare call
        assert fake_user.calls[-1] == (target, None, None)

    def test_mutate_with_self_is_a_noop(self, fake_user):
        u = Unit('widget', color='red')
        u.set_reactivity(fake_user)
        u.mutate(u)
        assert u.color == 'red'
        assert fake_user.calls == []

    def test_accept_calls_existing_changed_handler(self):
        received = []
        u = Unit('widget', changed=lambda obj, val: received.append(val))
        u.accept(7)
        assert received == [7]

    def test_accept_sets_value_when_no_changed_handler(self):
        u = Unit('widget')
        u.accept(7)
        assert u.value == 7

    def test_delattr_removes_existing_attribute(self):
        u = Unit('widget', color='red')
        u.delattr('color')
        assert not hasattr(u, 'color')

    def test_delattr_on_missing_attribute_is_a_noop(self):
        u = Unit('widget')
        u.delattr('does_not_exist')  # must not raise


class TestUnitEqualityAndViews:
    def test_equality_is_identity_based(self):
        a = Unit('a')
        b = Unit('a')
        assert a == a
        assert (a == b) is False

    def test_equality_against_changed_proxy_unwraps(self):
        a = Unit('a')
        proxy = ChangedProxy(a, a)
        assert a == proxy

    def test_hash_is_identity_based(self):
        a = Unit('a')
        assert hash(a) == hash(a)
        assert {a}  # hashable, usable in a set

    def test_params_returns_value(self):
        u = Unit('widget', value=[1, 2])
        assert u.params == u.value

    def test_compact_view_formats_name_and_value(self):
        u = Unit('widget', value=42)
        assert u.compact_view == 'widget : 42'

    def test_type_value_returns_python_type(self):
        u = Unit('widget', value=42, type='number')
        assert u.type_value is int

    def test_type_value_special_cases_date(self):
        u = Unit('widget', value='2024-01-01', type='date')
        assert u.type_value == 'date'

    def test_str_and_repr(self):
        u = Edit('widget')
        assert str(u) == 'Edit(widget)'
        assert repr(u) == 'Edit(widget)'


class TestUnitGetstate:
    def test_getstate_excludes_underscore_attributes(self):
        u = Unit('widget')
        u._private = 'hidden'
        state = u.__getstate__()
        assert '_private' not in state

    def test_getstate_replaces_action_handlers_with_true(self):
        u = Unit('widget', changed=lambda obj, val: val, delete=lambda obj, val: val)
        state = u.__getstate__()
        assert state['changed'] is True
        assert state['delete'] is True

    def test_getstate_keeps_non_action_values_as_is(self):
        u = Unit('widget', value=5, color='red')
        state = u.__getstate__()
        assert state['value'] == 5
        assert state['color'] == 'red'


class TestUnitAddChangedHandler:
    @pytest.mark.asyncio
    async def test_composes_with_default_set_value_handler(self):
        u = Unit('widget', value=1)
        seen = []
        u.add_changed_handler(lambda obj, val: seen.append(val))
        await u.changed(u, 99)
        assert u.value == 99
        assert seen == [99]

    @pytest.mark.asyncio
    async def test_composes_with_existing_changed_handler(self):
        order = []
        u = Unit('widget', changed=lambda obj, val: order.append(('first', val)))
        u.add_changed_handler(lambda obj, val: order.append(('second', val)))
        await u.changed(u, 5)
        assert order == [('first', 5), ('second', 5)]


class TestUnitEmit:
    @pytest.mark.asyncio
    async def test_noop_when_llm_off(self, llm_off, fake_get_property):
        u = Unit('widget', llm=True)
        result = await u.emit()
        assert result is None
        assert fake_get_property.calls == []

    @pytest.mark.asyncio
    async def test_noop_when_llm_attribute_not_set(self, llm_on, fake_get_property):
        u = Unit('widget')  # no `llm` attribute at all
        result = await u.emit()
        assert result is None
        assert fake_get_property.calls == []

    @pytest.mark.asyncio
    async def test_calls_get_property_and_sets_value(self, llm_on, fake_get_property):
        fake_get_property.result = 'computed'
        dep = Unit('dep', value='context value')
        u = Unit('widget', value='', llm=False, type='string')
        u._llm_dependencies = [dep]
        result = await u.emit()
        assert result is u
        assert u.value == 'computed'
        name, context, type_, options = fake_get_property.calls[0]
        assert name == 'widget'
        assert 'dep' in context

    @pytest.mark.asyncio
    async def test_exact_mode_requires_all_dependencies_filled(self, llm_on, fake_get_property):
        empty_dep = Unit('dep1', value='')
        filled_dep = Unit('dep2', value='x')
        u = Unit('widget', value='', llm=True, type='string')  # llm=True -> exact match required
        u._llm_dependencies = [empty_dep, filled_dep]
        result = await u.emit()
        assert result is None
        assert fake_get_property.calls == []

    @pytest.mark.asyncio
    async def test_non_exact_mode_proceeds_with_partial_dependencies(self, llm_on, fake_get_property):
        empty_dep = Unit('dep1', value='')
        filled_dep = Unit('dep2', value='x')
        u = Unit('widget', value='', llm=False, type='string')  # llm=False -> best-effort
        u._llm_dependencies = [empty_dep, filled_dep]
        result = await u.emit()
        assert result is u
        assert fake_get_property.calls  # went ahead despite the empty dependency


# ──────────────────────────────────────────────────────────────────────── #
#  smart_complete                                                          #
# ──────────────────────────────────────────────────────────────────────── #

class TestSmartComplete:
    def test_matches_substring_case_insensitively(self):
        complete = smart_complete(['Banana', 'Apple', 'Grape'])
        assert complete(None, 'AN') == ['Banana']

    def test_no_match_returns_empty_list(self):
        complete = smart_complete(['Banana', 'Apple'])
        assert complete(None, 'zzz') == []

    def test_min_input_length_gates_short_queries(self):
        complete = smart_complete(['Banana', 'Apple'], min_input_length=3)
        assert complete(None, 'ap') == []
        assert complete(None, 'app') == ['Apple']

    def test_max_output_length_truncates(self):
        complete = smart_complete(['aa', 'ab', 'ac', 'ad'], max_output_length=2)
        assert len(complete(None, 'a')) == 2

    def test_results_sorted_by_match_position_then_alphabetically(self):
        complete = smart_complete(['banana', 'cabana', 'ananas'])
        # 'ananas' matches at position 0, 'banana' at 1, 'cabana' at 2
        assert complete(None, 'ana') == ['ananas', 'banana', 'cabana']


# ──────────────────────────────────────────────────────────────────────── #
#  Edit                                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestEdit:
    def test_defaults_to_empty_string(self):
        e = Edit('field')
        assert e.type == 'string'
        assert e.value == ''
        assert e.x == 0

    def test_int_value_infers_number_type(self):
        e = Edit('field', 5)
        assert e.type == 'number'
        assert e.value == 5

    def test_float_value_infers_number_type(self):
        e = Edit('field', 5.5)
        assert e.type == 'number'

    def test_string_value_infers_string_type(self):
        e = Edit('field', 'hello')
        assert e.type == 'string'
        assert e.value == 'hello'

    def test_explicit_type_is_respected(self):
        e = Edit('field', type='date')
        assert e.type == 'date'
        assert e.value == ''  # not the number branch, so '' default applies

    def test_bool_value_is_treated_as_string_type(self):
        """Documents current, intentional behaviour: Edit distinguishes
        number from non-number with `type(value) == int or ... == float`
        rather than isinstance, specifically so that a bool (a `Switch`'s
        job, not an `Edit`'s) doesn't get classified as a number."""
        e = Edit('field', True)
        assert e.type == 'string'


# ──────────────────────────────────────────────────────────────────────── #
#  Text                                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestText:
    def test_value_mirrors_name(self):
        t = Text('label text')
        assert t.value == 'label text'
        assert t.type == 'string'
        assert t.edit is False

    def test_positional_value_argument_is_overridden_by_name(self):
        """Documents current behaviour: Text always forces value = name,
        even if a positional 'value' argument is supplied -- Text is a
        static label, not an editable field."""
        t = Text('label text', 'ignored')
        assert t.value == 'label text'


# ──────────────────────────────────────────────────────────────────────── #
#  Range                                                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestRange:
    def test_defaults(self):
        r = Range('slider')
        assert r.value == 1.0
        assert r.type == 'range'
        assert r.options == [-9.0, 11.0, 1]

    def test_explicit_value_drives_default_options(self):
        r = Range('slider', 5)
        assert r.value == 5
        assert r.options == [-5, 15, 1]

    def test_explicit_options_are_respected(self):
        r = Range('slider', 5, options=[0, 10, 1])
        assert r.options == [0, 10, 1]


# ──────────────────────────────────────────────────────────────────────── #
#  ContentScaler                                                           #
# ──────────────────────────────────────────────────────────────────────── #

class TestContentScaler:
    def test_defaults_with_no_arguments(self):
        cs = ContentScaler()
        assert cs.name == 'Scale content'
        assert cs.value == 1.0
        assert cs.options == [0.25, 3.0, 0.25]
        assert cs.changed == cs.scaler

    def test_name_only(self):
        cs = ContentScaler('Zoom')
        assert cs.name == 'Zoom'
        assert cs.value == 1.0

    def test_regression_name_and_value_positional_args(self):
        """Regression: ContentScaler used to re-pass `name` through *args a
        second time into Range.__init__/Unit.__init__, so a positional
        (name, value) call corrupted self.value into the *name string* and
        crashed building Range's numeric default `options` from it."""
        cs = ContentScaler('Zoom', 2.0)
        assert cs.name == 'Zoom'
        assert cs.value == 2.0

    def test_kwargs_only_call_still_works(self):
        """The actual call site in containers.py: no positional args at
        all, only kwargs -- must keep working exactly as before."""
        elements_called = []
        cs = ContentScaler(elements=lambda: elements_called)
        assert cs.name == 'Scale content'
        assert cs.elements() is elements_called

    def test_scaler_rescales_elements_proportionally(self):
        class FakeElement:
            def __init__(self, width, height):
                self.width = width
                self.height = height

        el = FakeElement(100.0, 50.0)
        cs = ContentScaler(elements=lambda: [el])
        cs.value = 1.0
        cs.scaler(cs, 2.0)
        assert el.width == 200.0
        assert el.height == 100.0
        assert cs.value == 2.0

    def test_scaler_without_elements_does_not_raise(self):
        cs = ContentScaler(elements=lambda: [])
        cs.scaler(cs, 2.0)  # must not raise
        assert cs.value == 2.0


# ──────────────────────────────────────────────────────────────────────── #
#  Button / CameraButton / UploadButton                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestButton:
    def test_defaults(self):
        b = Button('click me')
        assert b.type == 'command'
        assert b.value is None

    def test_handler_becomes_changed(self):
        def handler(obj, val):
            return val
        b = Button('click me', handler)
        assert b.changed is handler

    def test_explicit_type_is_not_overridden(self):
        b = Button('click me', type='fancy')
        assert b.type == 'fancy'

    def test_no_handler_means_no_changed_attribute(self):
        b = Button('click me')
        assert not hasattr(b, 'changed')


class TestCameraAndUploadButton:
    def test_camera_button_sets_type(self):
        b = CameraButton('take photo')
        assert b.type == 'camera'

    def test_upload_button_sets_type_and_default_width(self):
        b = UploadButton('upload file')
        assert b.type == 'uploader'
        assert b.width == 250.0

    def test_upload_button_respects_explicit_width(self):
        b = UploadButton('upload file', width=100.0)
        assert b.width == 100.0


# ──────────────────────────────────────────────────────────────────────── #
#  Image                                                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestImage:
    def test_defaults(self):
        img = Image('photo.png')
        assert img.type == 'image'
        assert img.value is False
        assert img.label == ''
        assert img.width == 300
        assert img.url == 'photo.png'

    def test_regression_single_character_name_does_not_raise(self):
        """Regression: `self.url[1] == ':'` used to be evaluated without
        checking the string was long enough, raising IndexError for any
        one-character name/url."""
        img = Image('a')
        assert img.url == 'a'

    def test_regression_empty_handler_not_confused_with_missing(self):
        img = Image('')
        assert img.url == ''

    def test_windows_path_gets_leading_slash_masked(self):
        img = Image('C:\\images\\photo.png')
        assert img.url == '/C:\\images\\photo.png'

    def test_explicit_url_kwarg_is_not_overridden_by_name(self):
        img = Image('photo.png', url='/static/photo.png')
        assert img.url == '/static/photo.png'

    def test_explicit_handler_becomes_changed(self):
        def handler(obj, val):
            return val
        img = Image('photo.png', handler=handler)
        assert img.changed is handler


# ──────────────────────────────────────────────────────────────────────── #
#  Video                                                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestVideo:
    def test_defaults_to_empty_fragments(self):
        v = Video('clip')
        assert v.type == 'video'
        assert v.fragments == []

    def test_explicit_fragments_respected(self):
        v = Video('clip', fragments=[{'start': 0, 'end': 1}])
        assert v.fragments == [{'start': 0, 'end': 1}]

    def test_two_instances_do_not_share_fragments_list(self):
        v1 = Video('clip1')
        v2 = Video('clip2')
        v1.fragments.append({'start': 0, 'end': 1})
        assert v2.fragments == []


# ──────────────────────────────────────────────────────────────────────── #
#  Sound                                                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestSound:
    def test_defaults_to_empty_dict_value(self):
        s = Sound('beep')
        assert s.type == 'sound'
        assert s.value == {}

    def test_regression_two_instances_do_not_share_default_value_dict(self):
        """Regression: Sound(value={}) used the same {} object as the
        default for every instance that didn't pass value= explicitly
        (classic Python mutable-default-argument bug). Mutating one
        instance's value dict used to leak into every sibling instance."""
        s1 = Sound('beep1')
        s2 = Sound('beep2')
        assert s1.value is not s2.value
        s1.value['position'] = 42
        assert s2.value == {}

    def test_explicit_value_is_respected(self):
        s = Sound('beep', value={'url': 'x.mp3', 'play': True})
        assert s.value == {'url': 'x.mp3', 'play': True}

    def test_explicit_handler_becomes_changed(self):
        def handler(obj, val):
            return val
        s = Sound('beep', handler=handler)
        assert s.changed is handler


# ──────────────────────────────────────────────────────────────────────── #
#  Chart                                                                   #
# ──────────────────────────────────────────────────────────────────────── #

class TestChart:
    def test_initial_value_becomes_option_then_value_resets_to_none(self):
        c = Chart('sales', {'series': [1, 2, 3]})
        assert c.type == 'chart'
        assert c.option == {'series': [1, 2, 3]}
        assert c.value is None

    def test_no_initial_value_gives_empty_option(self):
        c = Chart('sales')
        assert c.option == {}
        assert c.value is None


# ──────────────────────────────────────────────────────────────────────── #
#  Switch / Select / Tree / TextArea / HTML                                #
# ──────────────────────────────────────────────────────────────────────── #

class TestSwitch:
    def test_defaults(self):
        s = Switch('flag')
        assert s.value is False
        assert s.type == 'switch'

    def test_explicit_value_respected(self):
        s = Switch('flag', True)
        assert s.value is True


class TestSelect:
    def test_defaults_to_radio_for_few_options(self):
        s = Select('choice', options=['a', 'b'])
        assert s.type == 'radio'
        assert s.value is None

    def test_more_than_three_options_defaults_to_select(self):
        s = Select('choice', options=['a', 'b', 'c', 'd'])
        assert s.type == 'select'

    def test_no_options_defaults_to_empty_list_and_radio(self):
        s = Select('choice')
        assert s.options == []
        assert s.type == 'radio'

    def test_two_instances_do_not_share_options_list(self):
        s1 = Select('choice1')
        s2 = Select('choice2')
        s1.options.append('x')
        assert s2.options == []


class TestTree:
    def test_defaults(self):
        t = Tree('nodes')
        assert t.options == []
        assert t.value is None
        assert t.type == 'tree'


class TestTextArea:
    def test_defaults(self):
        ta = TextArea('notes')
        assert ta.type == 'text'
        assert ta.x == 0


class TestHTML:
    def test_defaults(self):
        h = HTML('content', '<b>hi</b>')
        assert h.type == 'html'
        assert h.value == '<b>hi</b>'
