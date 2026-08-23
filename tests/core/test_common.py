"""
Tests for common.py: the framework's pure-ish primitives -- flatten,
compose_handlers, ArgObject/ReceivedMessage, Message/TypeMessage and its
Warning/Error/Info/Answer shortcuts, delete_unit, set_defaults,
context_object, and a handful of small helpers.

Almost none of this needs a fixtures_app: these are either plain functions
operating on plain values, or small classes with no dependency on
screens/sessions. The exceptions (context_object, and compose_handlers'
interaction with a real call stack) still don't need a *User* -- just real
Python call frames, which any test function already provides.
"""
import asyncio

import pytest

from unisi.common import (
    ArgObject,
    Answer,
    Error,
    Info,
    Message,
    ReceivedMessage,
    Redesign,
    TypeMessage,
    Unishare,
    UpdateScreen,
    Warning,
    call_anysync,
    compose_handlers,
    context_object,
    delete_unit,
    empty_app,
    equal_dicts,
    flatten,
    get_default_args,
    index_of,
    is_callable,
    pretty4,
    set_defaults,
    strpath,
    toJson,
)


class FakeUnit:
    """Minimal stand-in for a Unit: delete_unit/compose_handlers only ever
    touch `.name` (and identity), never anything Unit-specific."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"FakeUnit({self.name!r})"


class TestFlatten:
    def test_flat_list_is_unchanged(self):
        assert list(flatten([1, 2, 3])) == [1, 2, 3]

    def test_nested_lists_are_flattened(self):
        assert list(flatten([1, [2, 3], [4, [5, 6]]])) == [1, 2, 3, 4, 5, 6]

    def test_tuples_are_flattened_too(self):
        assert list(flatten((1, (2, 3)))) == [1, 2, 3]

    def test_mixed_list_and_tuple_nesting(self):
        assert list(flatten([1, (2, [3, 4]), 5])) == [1, 2, 3, 4, 5]

    def test_multiple_top_level_arguments(self):
        # flatten(*arr) -- several separate arguments, not just one list.
        assert list(flatten(1, [2, 3], 4)) == [1, 2, 3, 4]

    def test_no_arguments_yields_nothing(self):
        assert list(flatten()) == []

    def test_non_list_leaf_values_pass_through(self):
        unit = FakeUnit("x")
        assert list(flatten([unit, None, "text", 5])) == [unit, None, "text", 5]

    def test_empty_nested_containers_contribute_nothing(self):
        assert list(flatten([1, [], (), [2]])) == [1, 2]


class TestIndexOf:
    def test_found_returns_position(self):
        assert index_of([10, 20, 30], 20) == 1

    def test_not_found_returns_minus_one(self):
        assert index_of([10, 20, 30], 99) == -1

    def test_empty_list_returns_minus_one(self):
        assert index_of([], 1) == -1

    def test_first_match_wins_for_duplicates(self):
        assert index_of([5, 5, 5], 5) == 0


class TestCallAnysync:
    @pytest.mark.asyncio
    async def test_calls_plain_sync_function(self):
        def add(a, b):
            return a + b

        assert await call_anysync(add, 2, 3) == 5

    @pytest.mark.asyncio
    async def test_awaits_async_function(self):
        async def add(a, b):
            return a + b

        assert await call_anysync(add, 2, 3) == 5

    @pytest.mark.asyncio
    async def test_awaits_bound_async_method(self):
        class Adder:
            async def add(self, a, b):
                return a + b

        assert await call_anysync(Adder().add, 2, 3) == 5

    @pytest.mark.asyncio
    async def test_no_extra_params_is_fine(self):
        called = []

        def handler():
            called.append(True)
            return "ok"

        assert await call_anysync(handler) == "ok"
        assert called == [True]

    @pytest.mark.asyncio
    async def test_callable_object_with_async_dunder_call_is_awaited(self):
        """Regression test: a callable *instance* whose __call__ is a
        coroutine function is a legitimate handler shape (a natural way to
        write a stateful callback) but asyncio.iscoroutinefunction(handler)
        -- the original implementation's whole detection mechanism -- is
        False for an *instance*, even when its __call__ is async. That used
        to make call_anysync invoke the object synchronously, hand back the
        resulting coroutine object as if it were the real return value, and
        never await it -- so `result` was a coroutine, not the handler's
        actual answer, and Python would eventually warn "coroutine was
        never awaited". call_anysync must call first and await whatever
        comes back if it's awaitable, regardless of what shape the
        callable itself is.
        """

        class AsyncCallable:
            def __init__(self):
                self.seen = []

            async def __call__(self, a, b):
                self.seen.append((a, b))
                return a * b

        handler = AsyncCallable()
        result = await call_anysync(handler, 3, 4)

        assert result == 12
        assert handler.seen == [(3, 4)]
        assert not asyncio.iscoroutine(result)

    @pytest.mark.asyncio
    async def test_functools_partial_over_async_function_is_awaited(self):
        import functools

        async def add(a, b, c):
            return a + b + c

        partial_add = functools.partial(add, 1)
        assert await call_anysync(partial_add, 2, 3) == 6


class TestStrpath:
    def test_joins_with_at_sign(self):
        assert strpath(["Leaf", "Block", "Root"]) == "Leaf@Block@Root"

    def test_single_element(self):
        assert strpath(["Only"]) == "Only"

    def test_empty_list(self):
        assert strpath([]) == ""


class TestComposeHandlers:
    @pytest.mark.asyncio
    async def test_single_handler_result_is_returned_as_a_list(self):
        target = FakeUnit("target")

        def h(obj, value):
            return target

        composed = compose_handlers(h)
        assert await composed(FakeUnit("src"), "v") == [target]

    @pytest.mark.asyncio
    async def test_handler_returning_none_yields_none(self):
        def h(obj, value):
            return None

        composed = compose_handlers(h)
        assert await composed(FakeUnit("src"), "v") is None

    @pytest.mark.asyncio
    async def test_handler_returning_falsy_non_none_yields_none(self):
        # `elif result:` -- an empty list/0/'' result is treated the same
        # as None, not added to the output set.
        def h(obj, value):
            return []

        composed = compose_handlers(h)
        assert await composed(FakeUnit("src"), "v") is None

    @pytest.mark.asyncio
    async def test_updatescreen_short_circuits_remaining_handlers(self):
        calls = []

        def h1(obj, value):
            calls.append("h1")
            return UpdateScreen

        def h2(obj, value):
            calls.append("h2")
            return None

        composed = compose_handlers(h1, h2)
        result = await composed(FakeUnit("src"), "v")

        assert result == UpdateScreen
        assert calls == ["h1"]  # h2 never ran

    @pytest.mark.asyncio
    async def test_redesign_short_circuits_remaining_handlers(self):
        def h1(obj, value):
            return Redesign

        def h2(obj, value):
            raise AssertionError("must not run after Redesign")

        composed = compose_handlers(h1, h2)
        assert await composed(FakeUnit("src"), "v") == Redesign

    @pytest.mark.asyncio
    async def test_results_from_multiple_handlers_are_merged(self):
        a, b = FakeUnit("a"), FakeUnit("b")

        def h1(obj, value):
            return a

        def h2(obj, value):
            return b

        composed = compose_handlers(h1, h2)
        result = await composed(FakeUnit("src"), "v")

        assert set(result) == {a, b}

    @pytest.mark.asyncio
    async def test_list_result_is_flattened_into_the_merged_set(self):
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")

        def h1(obj, value):
            return [a, [b, c]]

        composed = compose_handlers(h1)
        result = await composed(FakeUnit("src"), "v")

        assert set(result) == {a, b, c}

    @pytest.mark.asyncio
    async def test_duplicate_results_across_handlers_are_deduplicated(self):
        shared = FakeUnit("shared")

        def h1(obj, value):
            return shared

        def h2(obj, value):
            return shared

        composed = compose_handlers(h1, h2)
        result = await composed(FakeUnit("src"), "v")

        assert result == [shared]

    @pytest.mark.asyncio
    async def test_later_handler_still_receives_the_original_obj(self):
        """Regression test for a variable-shadowing bug: the merge loop
        `for obj in flatten(result): objs.add(obj)` used to reuse the
        composed function's own `obj` parameter as its loop variable. When
        an earlier handler returned a list/tuple result, that inner loop
        silently overwrote `obj` in the enclosing `compose()` scope (Python
        for-loops don't create a new scope) with the *last item of that
        result* -- so every subsequent handler in the chain was invoked
        with some unrelated changed-unit as its first argument instead of
        the actual unit the event was about. Multi-handler composition
        (registering a second @handle(...) for a unit/event that already
        has one) is a documented, supported feature (see handle() in
        server.py), so this is reachable any time the first handler in a
        chain returns other changed units -- a common, idiomatic thing for
        a handler to do.
        """
        original = FakeUnit("original-source")
        seen_by_h2 = []

        def h1(obj, value):
            # A realistic handler: reports a couple of *other* units it
            # changed as a side effect (a common, idiomatic return shape).
            return [FakeUnit("other_a"), FakeUnit("other_b")]

        def h2(obj, value):
            seen_by_h2.append(obj)
            return None

        composed = compose_handlers(h1, h2)
        await composed(original, "v")

        assert seen_by_h2 == [original]

    @pytest.mark.asyncio
    async def test_three_handlers_all_receive_the_original_obj(self):
        original = FakeUnit("src")
        seen = []

        def h1(obj, value):
            seen.append(("h1", obj))
            return [FakeUnit("x"), FakeUnit("y")]

        def h2(obj, value):
            seen.append(("h2", obj))
            return FakeUnit("z")

        def h3(obj, value):
            seen.append(("h3", obj))
            return None

        composed = compose_handlers(h1, h2, h3)
        await composed(original, "v")

        assert seen == [("h1", original), ("h2", original), ("h3", original)]

    @pytest.mark.asyncio
    async def test_async_handlers_are_awaited(self):
        async def h(obj, value):
            return FakeUnit("async-result")

        composed = compose_handlers(h)
        result = await composed(FakeUnit("src"), "v")
        assert result[0].name == "async-result"


class TestEqualDicts:
    def test_equal_dicts_are_equal(self):
        assert equal_dicts({"a": 1, "b": 2}, {"a": 1, "b": 2})

    def test_different_values_are_not_equal(self):
        assert not equal_dicts({"a": 1}, {"a": 2})

    def test_different_keys_are_not_equal(self):
        assert not equal_dicts({"a": 1}, {"b": 1})

    def test_different_lengths_are_not_equal(self):
        assert not equal_dicts({"a": 1}, {"a": 1, "b": 2})

    def test_two_empty_dicts_are_equal(self):
        assert equal_dicts({}, {})

    def test_key_order_does_not_matter(self):
        assert equal_dicts({"a": 1, "b": 2}, {"b": 2, "a": 1})


class TestArgObject:
    def test_kwargs_become_attributes(self):
        obj = ArgObject(a=1, b="x")
        assert obj.a == 1
        assert obj.b == "x"

    def test_unknown_attribute_returns_none(self):
        obj = ArgObject(a=1)
        assert obj.unknown_prop is None

    def test_hasattr_is_always_true_by_design(self):
        # __getattr__ never raises AttributeError, so hasattr() can't
        # distinguish "declared" from "undeclared" -- this is the
        # documented contract ("return None for unknown props"), matching
        # a JS-style props bag. Pinning it down so a future change doesn't
        # silently flip this without anyone noticing.
        obj = ArgObject(a=1)
        assert hasattr(obj, "a")
        assert hasattr(obj, "totally_made_up")

    def test_no_kwargs_is_a_valid_empty_object(self):
        obj = ArgObject()
        assert obj.anything is None


class TestReceivedMessage:
    def test_fields_come_from_the_dict(self):
        msg = ReceivedMessage({"block": "B", "element": "E", "event": "changed", "value": 5})
        assert msg.block == "B"
        assert msg.element == "E"
        assert msg.event == "changed"
        assert msg.value == 5

    def test_missing_field_is_none_not_an_error(self):
        msg = ReceivedMessage({"block": "B"})
        assert msg.element is None
        assert msg.event is None

    def test_str_format(self):
        msg = ReceivedMessage({"block": "B", "element": "E", "event": "changed", "value": 5})
        assert str(msg) == "B/E->changed(5)"

    def test_screen_type_true_for_root_block_no_element(self):
        msg = ReceivedMessage({"block": "root", "element": None})
        assert msg.screen_type is True

    def test_screen_type_false_when_element_present(self):
        msg = ReceivedMessage({"block": "root", "element": "E"})
        assert msg.screen_type is False

    def test_screen_type_false_for_non_root_block(self):
        msg = ReceivedMessage({"block": "other", "element": None})
        assert msg.screen_type is False

    def test_voice_type_true_for_voice_block_no_element(self):
        msg = ReceivedMessage({"block": "voice", "element": None})
        assert msg.voice_type is True

    def test_voice_type_false_for_other_block(self):
        msg = ReceivedMessage({"block": "root", "element": None})
        assert msg.voice_type is False


class TestToJson:
    def test_encodes_plain_dict(self):
        assert toJson({"a": 1}) == '{"a": 1}'

    def test_encodes_arg_object_by_its_dict(self):
        obj = ArgObject(a=1, b=2)
        # unpicklable=False -- plain JSON, no jsonpickle type tags.
        assert toJson(obj) == '{"a": 1, "b": 2}'

    def test_encodes_list(self):
        assert toJson([1, 2, 3]) == "[1, 2, 3]"

    def test_non_string_dict_keys_are_plain_stringified_not_json_prefixed(self):
        # Pins the deliberate keys=False choice: a jsonpickle 5.0
        # keys=True default would instead emit {"json://1": "int-key"}
        # (its round-trip-preserving encoding for non-string keys), which
        # a plain JS/JSON consumer -- toJson's only real audience, since
        # it always encodes unpicklable=False -- has no way to make sense
        # of.
        assert toJson({1: "int-key"}) == '{"1": "int-key"}'

    def test_does_not_emit_the_jsonpickle_keys_deprecation_warning(self, recwarn):
        toJson({"a": 1})
        assert not any(
            issubclass(w.category, DeprecationWarning) and "keys will default to True" in str(w.message)
            for w in recwarn.list
        )


class TestSetDefaults:
    def test_missing_attribute_gets_the_default(self):
        class Obj:
            pass

        obj = Obj()
        set_defaults(obj, {"x": 1})
        assert obj.x == 1

    def test_existing_attribute_is_not_overwritten(self):
        class Obj:
            x = "already set"

        obj = Obj()
        set_defaults(obj, {"x": "default"})
        assert obj.x == "already set"

    def test_existing_falsy_attribute_is_still_left_alone(self):
        # hasattr(), not "is truthy" -- an explicit 0/False/'' must survive.
        class Obj:
            x = 0

        obj = Obj()
        set_defaults(obj, {"x": 99})
        assert obj.x == 0

    def test_multiple_defaults_applied_independently(self):
        class Obj:
            already = "kept"

        obj = Obj()
        set_defaults(obj, {"already": "overwritten?", "new_one": "added"})
        assert obj.already == "kept"
        assert obj.new_one == "added"

    def test_works_on_a_plain_namespace_object(self):
        import types

        ns = types.SimpleNamespace()
        set_defaults(ns, {"a": 1, "b": 2})
        assert ns.a == 1 and ns.b == 2


class TestPretty4:
    def test_underscore_becomes_space(self):
        assert pretty4("my_name") == "My name"

    def test_leading_underscore_is_stripped(self):
        assert pretty4("_private") == "Private"

    def test_first_letter_capitalized(self):
        assert pretty4("lowercase") == "Lowercase"

    def test_already_capitalized_is_unchanged(self):
        assert pretty4("Already") == "Already"

    def test_multiple_underscores(self):
        assert pretty4("a_b_c") == "A b c"


class TestIsCallable:
    def test_plain_function_is_callable(self):
        def f():
            pass

        assert is_callable(f)

    def test_bound_method_is_callable(self):
        class C:
            def m(self):
                pass

        assert is_callable(C().m)

    def test_lambda_is_callable(self):
        assert is_callable(lambda: None)

    def test_plain_object_is_not_callable(self):
        assert not is_callable(object())

    def test_string_is_not_callable(self):
        assert not is_callable("text")

    def test_callable_instance_is_callable(self):
        class Callable:
            def __call__(self):
                pass

        assert is_callable(Callable())


class TestContextObject:
    def test_finds_matching_instance_up_the_call_stack(self):
        class Marker:
            pass

        marker = Marker()

        def inner():
            return context_object(Marker)

        def outer(m):
            return inner()

        assert outer(marker) is marker

    def test_returns_none_when_nothing_matches(self):
        class Marker:
            pass

        def inner():
            return context_object(Marker)

        assert inner() is None

    def test_finds_self_on_a_method_call(self):
        class Marker:
            def call_inner(self):
                return context_object(Marker)

        marker = Marker()
        assert marker.call_inner() is marker

    def test_only_the_first_positional_argument_is_checked(self):
        class Marker:
            pass

        marker = Marker()

        def inner():
            return context_object(Marker)

        def outer(_first, m):
            # `m` is the SECOND positional arg -- context_object only
            # looks at each frame's first one, so this frame doesn't match
            # even though a Marker instance is right there in scope.
            return inner()

        assert outer("not-a-marker", marker) is None

    def test_nearest_frame_wins(self):
        class Marker:
            pass

        outer_marker = Marker()
        inner_marker = Marker()

        def innermost():
            return context_object(Marker)

        def inner(m):
            return innermost()

        def outer(m):
            return inner(inner_marker)

        assert outer(outer_marker) is inner_marker


class TestGetDefaultArgs:
    def test_collects_only_parameters_with_defaults(self):
        def f(a, b, c=10, d="hello"):
            pass

        assert get_default_args(f) == {"c": 10, "d": "hello"}

    def test_no_defaults_returns_empty_dict(self):
        def f(a, b):
            pass

        assert get_default_args(f) == {}

    def test_works_on_a_bound_method(self):
        class F:
            def example_function(self, a, b, c=10, d="hello"):
                pass

        assert get_default_args(F().example_function) == {"c": 10, "d": "hello"}


class TestUnishare:
    def test_default_context_user_returns_none(self):
        # Unishare's own module-level default, before server.py overrides
        # it with the real context_user() -- pinning down that the
        # fallback itself is safe to call with zero setup.
        assert Unishare.context_user() is None

    def test_sessions_starts_as_a_dict(self):
        assert isinstance(Unishare.sessions, dict)

    def test_unknown_attribute_is_none(self):
        assert Unishare.totally_made_up_attribute is None


class TestMessage:
    def test_updates_wrap_each_unit_as_data(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        msg = Message(a, b)
        assert msg.updates == [{"data": a}, {"data": b}]

    def test_default_type_is_update(self):
        assert Message().type == "update"

    def test_explicit_type_is_kept(self):
        assert Message(type="custom").type == "custom"

    def test_no_user_means_no_path_filling(self):
        a = FakeUnit("a")
        msg = Message(a)
        assert "path" not in msg.updates[0]

    def test_fill_paths4_adds_path_when_found(self):
        a = FakeUnit("a")

        class FakeUserWithPath:
            def find_path(self, data):
                return ["a", "Root"]

        msg = Message(a, user=FakeUserWithPath())
        assert msg.updates[0]["path"] == ["a", "Root"]

    def test_fill_paths4_drops_updates_with_no_path(self):
        a, b = FakeUnit("a"), FakeUnit("b")

        class FakeUserPathForOnlyB:
            def find_path(self, data):
                return ["b", "Root"] if data is b else None

        msg = Message(a, b, user=FakeUserPathForOnlyB())
        assert msg.updates == [{"data": b, "path": ["b", "Root"]}]

    def test_contains_true_for_included_unit(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        msg = Message(a, b)
        assert msg.contains(a) is True

    def test_contains_false_for_excluded_unit(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        msg = Message(a)
        assert msg.contains(b) is False

    def test_contains_uses_identity_not_equality(self):
        # Two distinct FakeUnit instances with the same name must not be
        # confused -- `unit is update['data']`, not `==`.
        a1 = FakeUnit("same-name")
        a2 = FakeUnit("same-name")
        msg = Message(a1)
        assert msg.contains(a2) is False


class TestTypeMessageShortcuts:
    def test_warning_sets_type_and_value(self):
        w = Warning("careful")
        assert w.type == "warning"
        assert w.value == "careful"

    def test_error_sets_type_and_value(self):
        e = Error("broken")
        assert e.type == "error"
        assert e.value == "broken"

    def test_info_sets_type_and_value(self):
        i = Info("fyi")
        assert i.type == "info"
        assert i.value == "fyi"

    def test_value_is_stringified(self):
        e = Error(404)
        assert e.value == "404"

    def test_extra_data_units_are_wrapped_as_updates(self):
        a = FakeUnit("a")
        e = Error("broken", a)
        assert e.updates == [{"data": a}]

    def test_answer_sets_type_value_and_message(self):
        original_msg = ReceivedMessage({"block": "B", "element": "E", "event": "complete"})
        ans = Answer("complete", original_msg, "the result")
        assert ans.type == "complete"
        assert ans.value == "the result"
        assert ans.message is original_msg


class TestDeleteUnit:
    """delete_unit(units, name) -> (found, updated_units): it never
    mutates `units` in place (a plain list *could* be popped from, but a
    tuple can't be shrunk, and `units` itself may legitimately be a tuple
    -- see the function's own docstring) -- every level is rebuilt fresh,
    and the caller is expected to use the returned `updated_units` rather
    than the original object.

    delete_unit's only real caller is containers.py's Block.close() (the
    closable-Block handler); the end-to-end integration with real
    Block/Screen/User objects -- including the self._user vs.
    Unishare.context_user() fallback and unwrapping a reactive screen's
    ChangedProxy-wrapped blocks -- is covered in
    tests/units/test_containers.py::TestBlockClosable instead of being
    duplicated here. This class sticks to delete_unit's own contract in
    isolation, since it's still common.py's own function to cover.
    """

    def test_deletes_top_level_match_from_a_list(self):
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")
        units = [a, b, c]
        found, result = delete_unit(units, "b")
        assert found is True
        assert result == [a, c]
        assert units == [a, b, c]  # the original is untouched

    def test_returns_false_when_not_found(self):
        units = [FakeUnit("a")]
        found, result = delete_unit(units, "missing")
        assert found is False
        assert result == units

    def test_unmatched_call_still_rebuilds_a_fresh_container(self):
        # "unchanged" means equal contents, not the same list object --
        # the function always rebuilds, whether or not it found anything.
        units = [FakeUnit("a")]
        found, result = delete_unit(units, "missing")
        assert result is not units

    def test_empty_list_returns_false(self):
        found, result = delete_unit([], "anything")
        assert found is False
        assert result == []

    def test_deletes_from_a_nested_list(self):
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")
        units = [a, [b, c]]
        found, result = delete_unit(units, "b")
        assert found is True
        assert result == [a, [c]]
        assert units == [a, [b, c]]  # original nested list untouched too

    def test_nested_list_removed_entirely_once_empty(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        found, result = delete_unit([a, [b]], "b")
        assert found is True
        assert result == [a]

    def test_only_the_first_match_is_removed(self):
        a1 = FakeUnit("dup")
        a2 = FakeUnit("dup")
        found, result = delete_unit([a1, a2], "dup")
        assert found is True
        assert result == [a2]

    def test_stops_after_first_deletion_in_nested_structure(self):
        a, b, c = FakeUnit("x"), FakeUnit("y"), FakeUnit("x")
        found, result = delete_unit([[a], [b, c]], "x")
        assert found is True
        # [a] becomes [] once `a` is removed, and the emptied sublist is
        # itself pruned from the parent -- so the *second* "x" (c, inside
        # the second sublist) is left completely untouched.
        assert result == [[b, c]]

    def test_nested_tuple_child_is_deleted_without_crashing(self):
        """Regression test: a nested *tuple* group used to crash with
        AttributeError ('tuple' object has no attribute 'pop') the moment
        the target was found directly inside it, back when delete_unit
        mutated in place. docs/voicecom.md documents that screen.blocks
        (and, by the same isinstance(x, list | tuple) convention used
        throughout units.py/containers.py, any nested layout group) may
        legitimately be a tuple rather than a list, so this is a real,
        reachable shape, not a synthetic one.
        """
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")
        found, result = delete_unit([a, (b, c)], "b")
        assert found is True
        assert result == [a, (c,)]

    def test_nested_tuple_emptied_by_deletion_is_removed_from_parent(self):
        a, b = FakeUnit("a"), FakeUnit("b")
        found, result = delete_unit([a, (b,)], "b")
        assert found is True
        assert result == [a]

    def test_nested_tuple_sibling_survives_as_a_tuple(self):
        # The rebuilt slot is a *new* tuple, not silently turned into a
        # list -- callers relying on isinstance(x, tuple) for that slot
        # elsewhere must keep seeing a tuple.
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")
        found, result = delete_unit([(a, b, c)], "b")
        assert found is True
        assert isinstance(result[0], tuple)
        assert result[0] == (a, c)

    def test_top_level_tuple_is_supported(self):
        """The new, non-mutating design's main advantage over popping in
        place: `units` itself can be a tuple, not just something nested
        inside a list -- e.g. a screen module written as
        `blocks = block_a, block_b` (a bare tuple), which docs/voicecom.md
        confirms is a legitimate shape for screen.blocks.
        """
        a, b, c = FakeUnit("a"), FakeUnit("b"), FakeUnit("c")
        found, result = delete_unit((a, b, c), "b")
        assert found is True
        assert result == (a, c)
        assert isinstance(result, tuple)

    def test_top_level_tuple_not_found_stays_a_tuple(self):
        a = FakeUnit("a")
        found, result = delete_unit((a,), "missing")
        assert found is False
        assert result == (a,)
        assert isinstance(result, tuple)

    def test_deeply_nested_tuple_inside_list_inside_tuple(self):
        a, b, sibling = FakeUnit("a"), FakeUnit("b"), FakeUnit("sibling")
        inner = [a, b]
        units = (sibling, inner)
        found, result = delete_unit(units, "a")
        assert found is True
        assert result == (sibling, [b])
        assert inner == [a, b]  # the original inner list is untouched


class TestEmptyApp:
    def test_has_expected_static_shape(self):
        assert empty_app.type == "screen"
        assert empty_app.blocks == []
        assert empty_app.toolbar == []
        assert empty_app.name == ""

    def test_unknown_attribute_still_falls_back_to_none(self):
        # empty_app is an ArgObject too.
        assert empty_app.not_a_real_field is None
