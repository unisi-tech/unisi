"""
Tests for persist.py's module-level helper functions: pure, no User/screen/DB
needed at all. Import straight from unisi.persist -- these are underscore-
prefixed (module-private) on purpose, so this file is explicitly testing
internals, not the public User API surface (that's every other file here).
"""
import pytest

from unisi.persist import (
    _encode_context_key,
    _escape_like,
    _is_flag_persist,
    _path_key,
    _split_template,
    _template_to_like,
)


class TestIsFlagPersist:
    def test_true_is_a_flag(self):
        assert _is_flag_persist(True) is True

    def test_false_is_not_a_flag(self):
        assert _is_flag_persist(False) is False

    def test_none_is_not_a_flag(self):
        assert _is_flag_persist(None) is False

    def test_a_callable_is_not_a_flag(self):
        # persist=<function> (keyed persist) is a different mechanism entirely --
        # _is_flag_persist must not treat it as positional persist=True.
        assert _is_flag_persist(lambda: ("x",)) is False

    def test_truthy_non_bool_also_counts_as_a_flag(self):
        # _is_flag_persist checks "truthy and not callable", not `is True`
        # specifically -- persist=1 or persist="yes" behave the same as
        # persist=True. Slightly more permissive than "classic boolean
        # persist=True" might suggest; documenting the actual behavior here.
        assert _is_flag_persist(1) is True
        assert _is_flag_persist("yes") is True


class TestEncodeContextKey:
    def test_single_value_has_no_wrapping(self):
        assert _encode_context_key(("Dog",)) == "Dog"

    def test_single_numeric_value(self):
        assert _encode_context_key((123,)) == "123"

    def test_multi_value_is_comma_joined(self):
        assert _encode_context_key(("London", 123)) == "London,123"

    def test_three_values(self):
        assert _encode_context_key(("a", "b", "c")) == "a,b,c"

    def test_comma_inside_a_value_is_escaped(self):
        encoded = _encode_context_key(("a,b", "c"))
        assert encoded == "a\\,b,c"

    def test_backslash_inside_a_value_is_escaped(self):
        encoded = _encode_context_key(("a\\b",))
        assert encoded == "a\\\\b"

    def test_escaping_prevents_collisions_between_different_tuples(self):
        # ('a,b', 'c') and ('a', 'b,c') would both read 'a,b,c' if the comma
        # inside a value weren't escaped -- the whole point of escaping.
        first = _encode_context_key(("a,b", "c"))
        second = _encode_context_key(("a", "b,c"))
        assert first != second

    def test_single_value_containing_a_comma_does_not_collide_with_a_pair(self):
        # ('a,b',) on its own vs ('a', 'b') -- escaping must apply even when
        # there's only one component and no join happens, or these collide too.
        single = _encode_context_key(("a,b",))
        pair = _encode_context_key(("a", "b"))
        assert single != pair

    def test_deterministic(self):
        assert _encode_context_key(("x", 1)) == _encode_context_key(("x", 1))


class TestPathKey:
    def test_string_passthrough(self):
        assert _path_key("Leaf@Block") == "Leaf@Block"

    def test_list_is_joined_with_at(self):
        assert _path_key(["Leaf", "Block", "Root"]) == "Leaf@Block@Root"

    def test_tuple_is_joined_with_at(self):
        assert _path_key(("Leaf", "Block")) == "Leaf@Block"

    def test_non_string_scalar_is_stringified(self):
        assert _path_key(None) == "None"


class TestSplitTemplate:
    """_split_template requires '..' and raises otherwise -- it's used
    directly by get_keys/remove_keys, which have no "exact match" mode (see
    get_objects/get_contexts instead, which check `'..' in template`
    themselves before ever calling this)."""

    def test_no_marker_raises(self):
        with pytest.raises(ValueError):
            _split_template("exact")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _split_template("")

    def test_prefix_only(self):
        assert _split_template("London,..") == ("London,", "")

    def test_suffix_only(self):
        assert _split_template("..,123") == ("", ",123")

    def test_prefix_and_suffix(self):
        assert _split_template("a..z") == ("a", "z")

    def test_bare_marker_is_prefix_and_suffix_search(self):
        assert _split_template("..") == ("", "")


class TestTemplateToLike:
    """_template_to_like(template) takes the raw template (not a pre-split
    prefix/suffix pair) and does the splitting *and* LIKE-escaping in one
    call."""

    def test_prefix_only_gets_a_trailing_wildcard(self):
        pattern = _template_to_like("London,..")
        assert pattern == "London,%"

    def test_suffix_only_gets_a_leading_wildcard(self):
        pattern = _template_to_like("..,123")
        assert pattern == "%,123"

    def test_percent_in_a_literal_fragment_is_escaped(self):
        # the literal '%' from a real key value must not act as a SQL wildcard
        pattern = _template_to_like("50%off,..")
        assert pattern == "50\\%off,%"

    def test_underscore_in_a_literal_fragment_is_escaped(self):
        pattern = _template_to_like("a_b,..")
        assert pattern == "a\\_b,%"

    def test_prefix_and_suffix_both_present(self):
        assert _template_to_like("a..z") == "a%z"

    def test_no_marker_raises_same_as_split_template(self):
        with pytest.raises(ValueError):
            _template_to_like("no marker here")


class TestEscapeLike:
    def test_plain_text_unchanged(self):
        assert _escape_like("hello") == "hello"

    def test_percent_escaped(self):
        assert _escape_like("50%") == "50\\%"

    def test_underscore_escaped(self):
        assert _escape_like("a_b") == "a\\_b"

    def test_backslash_escaped_first(self):
        # backslash must be escaped before % and _ are, or a literal
        # backslash preceding one of them would accidentally "protect" it
        assert _escape_like("a\\b") == "a\\\\b"

    def test_combination(self):
        assert _escape_like("50%_off\\now") == "50\\%\\_off\\\\now"
