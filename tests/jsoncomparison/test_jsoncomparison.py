"""
Tests for unisi/jsoncomparison/: Compare/Config (compare.py), the Error
hierarchy (errors.py), and Ignore (ignore.py) -- a small, self-contained
JSON-diffing library used by autotest.py (unisi/autotest.py's module-level
`comparator`) to compare a recorded/expected server response against a real
one.

Pure logic throughout: Compare.check(expected, actual) takes and returns
plain dicts/lists/scalars, so every test here constructs its own inputs
directly -- no fixtures_app, no User needed. See tests/autotest/ for the
autotest.py-level tests that exercise this same comparator against real
User/screen traffic (including a regression test for the AttributeError
crash the bug below used to cause there).

Writing this suite surfaced several genuine bugs, fixed alongside the
tests that pin the corrected behavior (see the comments at each fix site
in compare.py/ignore.py for the full rationale):

  * Compare(): every instance built without an explicit `config` shared
    the exact same mutable DEFAULT_CONFIG dict (and a caller-supplied
    config dict was never copied either), so report()'s file-writing path
    -- which used to pop 'name' straight out of the live config -- would
    silently and permanently break file output for every OTHER Compare
    instance sharing that dict, including a second .check() call on the
    very same instance. TestCompareReport below pins the fix: independent
    configs per instance, and repeated writes on one instance both land.
  * Compare._list_diff(): the `not isinstance(a, list)` branch fell off
    the end of the function without a `return`, silently producing None
    instead of an explain()-shaped dict. That None is real diff *content*
    (kept by _without_empties, since None != NO_DIFF), and later crashes
    autotest.py's diff-printing loop with AttributeError. See
    TestCompareListDiff.test_list_item_type_mismatch_reports_explain_dict.
  * Compare._diff(): two values sharing a type outside the six explicitly
    handled (dict/list/int/str/bool/float) -- e.g. two different tuples --
    always reported NO_DIFF regardless of whether they were actually
    equal. See TestCompareGenericTypeFallback.
  * Compare._diff()'s own type-mismatch check used `isinstance(a, t)`,
    which is True for a plain int actual value even when the expected
    value is a bool (bool subclasses int in Python) -- so an
    expected=int/actual=bool pair silently passed through to _int_diff's
    `==`, which also treats True as 1, reporting NO_DIFF for a real type
    difference. Only the reverse direction (expected=bool/actual=int) was
    ever caught. Fixed to an exact `type(a) is not t` check, symmetric in
    both directions. See test_bool_and_int_are_never_interchangeable.
  * Ignore._apply_regex_rule(): re.match() on a non-string value raised
    TypeError instead of the rule simply not matching. See
    TestIgnoreStringAndRegexRules.test_regex_rule_on_non_string_value_does_not_crash.

A few pieces of the module are exercised here even though nothing else in
the codebase currently calls them (Compare._max_diff/_min_diff, Config.merge,
the ValueNotFound error) -- they're part of the class's real, reachable
surface, just not yet wired up to anything; see each test's docstring.
"""
import copy
import io
import json
import os

import pytest

from unisi.jsoncomparison import (
    Compare,
    KeyNotExist,
    LengthsNotEqual,
    NO_DIFF,
    TypesNotEqual,
    UnexpectedKey,
    ValueNotFound,
    ValuesNotEqual,
)
from unisi.jsoncomparison.compare import DEFAULT_CONFIG
from unisi.jsoncomparison.config import Config
from unisi.jsoncomparison.ignore import Ignore


class TestCompareScalarTypes:
    """The four "leaf" types _diff dispatches on directly: int, str, bool,
    float. bool is deliberately distinct from int (type(e) is t uses an
    exact type check, not isinstance, specifically so True/False don't get
    treated as 1/0 -- see test_bool_and_int_are_never_interchangeable)."""

    def setup_method(self):
        self.c = Compare()

    def test_equal_ints(self):
        assert self.c.check({"v": 5}, {"v": 5}) == NO_DIFF

    def test_unequal_ints(self):
        diff = self.c.check({"v": 5}, {"v": 6})
        assert diff == {
            "v": {
                "_message": "Values not equal. Expected: <5>, received: <6>",
                "_expected": 5,
                "_received": 6,
            }
        }

    def test_equal_strings(self):
        assert self.c.check({"v": "hi"}, {"v": "hi"}) == NO_DIFF

    def test_unequal_strings(self):
        diff = self.c.check({"v": "hi"}, {"v": "bye"})
        assert diff["v"]["_message"] == "Values not equal. Expected: <hi>, received: <bye>"

    def test_equal_bools(self):
        assert self.c.check({"v": True}, {"v": True}) == NO_DIFF
        assert self.c.check({"v": False}, {"v": False}) == NO_DIFF

    def test_unequal_bools(self):
        diff = self.c.check({"v": True}, {"v": False})
        assert diff["v"]["_expected"] is True
        assert diff["v"]["_received"] is False

    def test_bool_and_int_are_never_interchangeable(self):
        """type(a) is not t (exact type check, not isinstance) in _diff --
        isinstance(True, int) is True in plain Python since bool subclasses
        int, so a naive isinstance check only caught this mix-up in one
        direction. Both directions must report a type mismatch here, never
        silently treat True as equal to 1 (or False as equal to 0)."""
        diff_e_bool = self.c.check({"v": True}, {"v": 1})
        assert diff_e_bool["v"]["_message"].startswith("Types not equal")
        assert diff_e_bool["v"]["_expected"] == "bool"
        assert diff_e_bool["v"]["_received"] == "int"

        diff_e_int = self.c.check({"v": 1}, {"v": True})
        assert diff_e_int["v"]["_message"].startswith("Types not equal")
        assert diff_e_int["v"]["_expected"] == "int"
        assert diff_e_int["v"]["_received"] == "bool"

        assert self.c.check({"v": False}, {"v": 0}) != NO_DIFF
        assert self.c.check({"v": 0}, {"v": False}) != NO_DIFF

    def test_equal_floats(self):
        assert self.c.check({"v": 1.5}, {"v": 1.5}) == NO_DIFF

    def test_unequal_floats_beyond_rounding(self):
        diff = self.c.check({"v": 1.0}, {"v": 1.5})
        assert diff["v"]["_message"] == "Values not equal. Expected: <1.0>, received: <1.5>"

    def test_floats_within_default_rounding_are_equal(self):
        """DEFAULT_CONFIG's types.float.allow_round is 2 -- a difference only
        in the 3rd decimal place is rounded away."""
        assert self.c.check({"v": 1.001}, {"v": 1.002}) == NO_DIFF

    def test_floats_rounding_can_be_disabled(self):
        c = Compare(config={"types": {"float": {"allow_round": None}}})
        diff = c.check({"v": 1.001}, {"v": 1.002})
        assert diff["v"]["_message"] == "Values not equal. Expected: <1.001>, received: <1.002>"

    def test_floats_rounding_precision_is_configurable(self):
        c = Compare(config={"types": {"float": {"allow_round": 0}}})
        assert c.check({"v": 1.4}, {"v": 1.49}) == NO_DIFF
        diff = c.check({"v": 1.0}, {"v": 2.0})
        assert diff != NO_DIFF


class TestCompareTypeMismatch:
    def setup_method(self):
        self.c = Compare()

    def test_dict_vs_list_at_root(self):
        diff = self.c.check({"a": 1}, [1, 2, 3])
        assert diff == {
            "_message": "Types not equal. Expected: <dict>, received: <list>",
            "_expected": "dict",
            "_received": "list",
        }

    def test_str_vs_int_field(self):
        diff = self.c.check({"v": "5"}, {"v": 5})
        assert diff["v"]["_expected"] == "str"
        assert diff["v"]["_received"] == "int"

    def test_none_vs_none_is_no_diff(self):
        assert self.c.check({"v": None}, {"v": None}) == NO_DIFF

    def test_none_vs_value_is_a_type_mismatch(self):
        diff = self.c.check({"v": None}, {"v": "x"})
        assert diff["v"]["_expected"] == "NoneType"
        assert diff["v"]["_received"] == "str"


class TestCompareDictDiff:
    def setup_method(self):
        self.c = Compare()

    def test_missing_key_reports_key_not_exist(self):
        diff = self.c.check({"a": 1, "b": 2}, {"a": 1})
        assert diff == {
            "b": {
                "_message": "Key does not exist. Expected: <b>",
                "_expected": "b",
                "_received": None,
            }
        }

    def test_extra_key_reports_unexpected_key(self):
        diff = self.c.check({"a": 1}, {"a": 1, "b": 2})
        assert diff == {
            "b": {
                "_message": "Unexpected key. Received: <b>",
                "_expected": None,
                "_received": "b",
            }
        }

    def test_nested_dict_diff_recurses(self):
        e = {"outer": {"inner": 1}}
        a = {"outer": {"inner": 2}}
        diff = self.c.check(e, a)
        assert diff["outer"]["inner"]["_message"] == "Values not equal. Expected: <1>, received: <2>"

    def test_name_is_attached_when_a_diff_exists(self):
        """_dict_diff adds '#name' (from the *expected* side's own 'name' key)
        onto its own diff dict whenever there IS a diff -- lets a caller like
        autotest.py's failure printer say *which* named element failed."""
        e = {"name": "MyButton", "value": "A"}
        a = {"name": "MyButton", "value": "B"}
        diff = self.c.check(e, a)
        assert diff["#name"] == "MyButton"
        assert diff["value"]["_message"] == "Values not equal. Expected: <A>, received: <B>"

    def test_name_is_absent_when_there_is_no_diff(self):
        e = {"name": "MyButton", "value": "A"}
        assert self.c.check(e, dict(e)) == NO_DIFF

    def test_name_is_absent_when_expected_has_no_name_key(self):
        diff = self.c.check({"value": "A"}, {"value": "B"})
        assert "#name" not in diff

    def test_dict_subclass_is_now_treated_as_a_type_mismatch(self):
        """_diff dispatches on an exact type check (type(a) is not t, fixed
        alongside the bool/int asymmetry above) rather than isinstance, so a
        dict subclass instance compared against a plain dict expectation (or
        vice versa) is now correctly flagged instead of silently falling
        through every branch to NO_DIFF regardless of actual content. Not
        reachable in practice either way: every value autotest.py compares
        here came out of json.loads(), which only ever produces plain
        dict/list/str/int/float/bool/None, never a subclass."""

        class DictLike(dict):
            pass

        e = DictLike(a=1)
        a_plain_dict_same_content = {"a": 1}
        diff = self.c.check(e, a_plain_dict_same_content)
        assert diff["_message"].startswith("Types not equal")


class TestCompareListDiff:
    def setup_method(self):
        self.c = Compare()

    def test_equal_lists(self):
        assert self.c.check({"v": [1, 2, 3]}, {"v": [1, 2, 3]}) == NO_DIFF

    def test_length_mismatch_is_reported_by_default(self):
        diff = self.c.check({"v": [1, 2, 3]}, {"v": [1, 2]})
        assert diff["v"]["_length"]["_message"] == "Lengths not equal. Expected <3>, received: <2>"

    def test_content_mismatch_at_a_position(self):
        diff = self.c.check({"v": [1, 2, 3]}, {"v": [1, 9, 3]})
        assert diff["v"]["_content"][1]["_message"] == "Values not equal. Expected: <2>, received: <9>"

    def test_length_checking_can_be_disabled(self):
        c = Compare(config={"types": {"list": {"check_length": False}}})
        assert c.check({"v": [1, 2, 3]}, {"v": [1, 2]}) == NO_DIFF

    def test_disabling_length_check_only_compares_the_overlapping_range(self):
        """Documents current behavior: with check_length off, extra items in
        either list beyond the shorter one's length are never itemized --
        only positions present in BOTH lists are compared. A real difference
        hiding in the non-overlapping tail is silently missed."""
        c = Compare(config={"types": {"list": {"check_length": False}}})
        e = {"v": [1, 2, 999]}
        a = {"v": [1, 2]}
        assert c.check(e, a) == NO_DIFF

    def test_list_of_dicts(self):
        e = {"v": [{"n": "a"}, {"n": "b"}]}
        a = {"v": [{"n": "a"}, {"n": "c"}]}
        diff = self.c.check(e, a)
        assert diff["v"]["_content"][1]["n"]["_message"] == "Values not equal. Expected: <b>, received: <c>"

    def test_list_of_lists(self):
        e = {"v": [[1, 2], [3, 4]]}
        a = {"v": [[1, 2], [3, 9]]}
        diff = self.c.check(e, a)
        assert diff["v"]["_content"][1]["_content"][1]["_message"] == \
            "Values not equal. Expected: <4>, received: <9>"

    def test_list_item_type_mismatch_reports_explain_dict(self):
        """Regression test for the missing `return` in _list_diff's type-
        mismatch branch: a list-of-lists whose actual side has a non-list
        item at some position used to make _list_diff fall off the end of
        the function, silently returning None instead of a diff dict for
        that position. That None was real content (see the module
        docstring), so `diff['v']['_content'][1]` must be a proper
        explain()-shaped dict here, not None -- see test_autotest.py's
        TestDiffPrintingRobustness for the downstream crash this used to
        cause in autotest.py's failure printer."""
        e = {"v": [[1, 2, 3], [4, 5]]}
        a = {"v": [[1, 2, 3], "not-a-list"]}
        diff = self.c.check(e, a)
        node = diff["v"]["_content"][1]
        assert node is not None
        assert node == {"type": {"_message": "Incompatible types"}}

    def test_dict_item_type_mismatch_reports_explain_dict(self):
        """The dict-shaped equivalent of the above (_dict_diff's own
        type-mismatch branch always returned properly; this pins that it
        still does)."""
        e = {"v": [{"n": "a"}, {"n": "b"}]}
        a = {"v": [{"n": "a"}, "not-a-dict"]}
        diff = self.c.check(e, a)
        assert diff["v"]["_content"][1] == {"type": {"_message": "Incompatible types"}}


class TestCompareGenericTypeFallback:
    """_diff's final fallback, for any type outside the six explicitly
    handled ones. Fixed to fall back to a real equality check instead of
    unconditionally reporting NO_DIFF -- unreachable via autotest.py's own
    json.loads()-sourced data (see the module docstring), but a real gap in
    Compare as a general-purpose comparator."""

    def setup_method(self):
        self.c = Compare()

    def test_equal_tuples_are_no_diff(self):
        assert self.c.check({"v": (1, 2)}, {"v": (1, 2)}) == NO_DIFF

    def test_unequal_tuples_are_now_reported(self):
        diff = self.c.check({"v": (1, 2)}, {"v": (1, 3)})
        assert diff == {
            "v": {
                "_message": "Values not equal. Expected: <(1, 2)>, received: <(1, 3)>",
                "_expected": (1, 2),
                "_received": (1, 3),
            }
        }

    def test_unequal_frozensets_are_now_reported(self):
        diff = self.c.check({"v": frozenset({1, 2})}, {"v": frozenset({1, 3})})
        assert diff != NO_DIFF
        assert diff["v"]["_message"].startswith("Values not equal")


class TestMaxMinDiff:
    """_max_diff/_min_diff aren't called anywhere else in unisi (nor
    exported from the package) but are real, reachable classmethods; these
    pin their current behavior. Despite the names, the difference isn't
    about the *size* of the diff each picks -- both look for a list item
    whose diff is no worse (len(dd) <= len(d)) than the best found so far,
    starting from the diff against an empty instance of the type. _min_diff
    stops at the very first such improvement/tie; _max_diff keeps scanning
    the whole list, so it can end up with a strictly better match found
    later on, as this test demonstrates."""

    def setup_method(self):
        self.c = Compare()

    def test_min_diff_stops_at_the_first_improvement(self):
        e = {"a": 1, "b": 2}
        candidates = [
            {"a": 1, "b": 3},  # one field differs -- an improvement over the empty-dict baseline
            {"a": 1, "b": 2},  # exact match, but never reached
        ]
        result = Compare._min_diff(e, candidates, self.c._dict_diff)
        assert result != NO_DIFF
        assert result["b"]["_received"] == 3

    def test_max_diff_keeps_scanning_and_can_find_an_exact_match_later(self):
        e = {"a": 1, "b": 2}
        candidates = [
            {"a": 1, "b": 3},  # one field differs
            {"a": 9, "b": 9},  # worse -- both fields differ, not accepted
            {"a": 1, "b": 2},  # exact match, reached because max_diff scans everything
        ]
        result = Compare._max_diff(e, candidates, self.c._dict_diff)
        assert result == NO_DIFF

    def test_no_matching_type_in_list_falls_back_to_the_empty_instance_diff(self):
        e = {"a": 1}
        result = Compare._min_diff(e, ["not-a-dict", 42], self.c._dict_diff)
        # every candidate in the list is skipped (wrong type), so the
        # baseline diff (e vs {}) is returned unchanged.
        assert result == self.c._dict_diff(e, {})


class TestConfig:
    def test_get_top_level_key(self):
        assert Config({"a": 1}).get("a") == 1

    def test_get_nested_path(self):
        assert Config({"a": {"b": {"c": 42}}}).get("a.b.c") == 42

    def test_get_missing_path_defaults_to_empty_dict(self):
        assert Config({"a": 1}).get("x.y.z") == {}

    def test_get_on_non_dict_intermediate_value_returns_false(self):
        """Config.get('a.b') where config['a'] is an int (not a dict): the
        second .get() call raises AttributeError internally, caught and
        turned into a plain False rather than propagating."""
        assert Config({"a": 5}).get("a.b") is False

    def test_get_preserves_falsy_stored_values(self):
        """.get(key, {}) only supplies the {} default when the key is
        genuinely absent -- an explicitly-stored False must come back as
        False, not be confused with "missing"."""
        assert Config({"output": {"console": False}}).get("output.console") is False

    def test_merge_updates_top_level_keys(self):
        """Config.merge isn't called anywhere in unisi currently, but is a
        real public method: a plain dict.update, so it only touches
        top-level keys and completely replaces (not deep-merges) any nested
        value with the same key."""
        config = Config({"a": 1, "b": {"x": 1}})
        config.merge({"b": {"y": 2}, "c": 3})
        assert config.config == {"a": 1, "b": {"y": 2}, "c": 3}

    def test_merge_does_not_affect_a_different_instance(self):
        c1 = Config({"a": 1})
        c2 = Config({"a": 1})
        c1.merge({"a": 2})
        assert c2.get("a") == 1


class TestIgnoreSpecialKeys:
    """The '_values'/'_list'/'_range' special keys, recognised only when
    they appear as a key inside a rules DICT (Ignore._apply_dictable_rule).
    Note _list's whole purpose is to apply a sub-rule to every item of a
    LIST value even though the enclosing rules node is a DICT (the {'_list':
    ...} rule) -- that obj/rules type "mismatch" is the intended mechanism,
    not something to be defended against."""

    def test_values_blacklists_dict_keys(self):
        c = Compare(rules={"obj": {"_values": ["secret"]}})
        e = {"obj": {"secret": "s1", "v": 2}}
        a = {"obj": {"secret": "s2", "v": 2}}
        assert c.check(e, a) == NO_DIFF

    def test_values_blacklists_list_items(self):
        c = Compare(rules={"v": {"_values": ["drop-me"]}})
        e = {"v": ["keep", "drop-me"]}
        a = {"v": ["keep"]}
        assert c.check(e, a) == NO_DIFF

    def test_list_applies_a_subrule_to_every_item(self):
        c = Compare(rules={"items": {"_list": {"ts": "*"}}})
        e = {"items": [{"ts": 1, "v": "a"}, {"ts": 2, "v": "b"}]}
        a = {"items": [{"ts": 999, "v": "a"}, {"ts": 888, "v": "b"}]}
        assert c.check(e, a) == NO_DIFF
        # a genuine difference in the non-ignored field is still caught
        a2 = {"items": [{"ts": 999, "v": "a"}, {"ts": 888, "v": "DIFFERENT"}]}
        diff = c.check(e, a2)
        assert diff != NO_DIFF

    def test_range_treats_both_in_range_values_as_equal(self):
        c = Compare(rules={"score": {"_range": [0, 100]}})
        assert c.check({"score": 5}, {"score": 95}) == NO_DIFF

    def test_range_flags_an_out_of_range_actual(self):
        c = Compare(rules={"score": {"_range": [0, 100]}})
        diff = c.check({"score": 5}, {"score": 150})
        assert diff != NO_DIFF

    def test_range_leaves_non_numeric_values_untouched(self):
        c = Compare(rules={"v": {"_range": [0, 100]}})
        assert c.check({"v": "not-a-number"}, {"v": "not-a-number"}) == NO_DIFF


class TestIgnoreStringAndRegexRules:
    def test_star_rule_deletes_the_key_from_both_sides(self):
        c = Compare(rules={"toolbar": "*"})
        e = {"toolbar": [1, 2, 3], "x": 1}
        a = {"toolbar": [9, 9], "x": 1}
        assert c.check(e, a) == NO_DIFF

    def test_star_rule_still_catches_other_differences(self):
        c = Compare(rules={"toolbar": "*"})
        diff = c.check({"toolbar": [1], "x": 1}, {"toolbar": [9], "x": 2})
        assert diff["x"]["_message"] == "Values not equal. Expected: <1>, received: <2>"

    def test_regex_rule_ignores_a_matching_value(self):
        c = Compare(rules={"id": {"_re": r"^tmp_"}})
        assert c.check({"id": "tmp_123", "x": 1}, {"id": "tmp_456", "x": 1}) == NO_DIFF

    def test_regex_rule_does_not_ignore_a_non_matching_value(self):
        c = Compare(rules={"id": {"_re": r"^tmp_"}})
        diff = c.check({"id": "tmp_123"}, {"id": "permanent_456"})
        assert diff != NO_DIFF

    def test_regex_rule_on_non_string_value_does_not_crash(self):
        """Regression test: re.match() used to be called directly on
        obj[key] with no type check, raising TypeError for a non-string
        value (an int, here). A regex rule simply doesn't match a
        non-string value now, instead of crashing the whole comparison."""
        c = Compare(rules={"id": {"_re": r"^tmp_"}})
        diff = c.check({"id": 5, "x": 1}, {"id": 5, "x": 1})
        assert diff == NO_DIFF  # values are equal anyway, so nothing to ignore

        diff2 = c.check({"id": 5, "x": 1}, {"id": 6, "x": 1})
        assert diff2 != NO_DIFF  # a genuine difference isn't masked either


class TestErrors:
    """The Error subclasses' .message/.explain() output. KeyNotExist,
    UnexpectedKey and LengthsNotEqual are already covered indirectly via
    Compare above; these test each class directly and in isolation,
    including ValueNotFound, which nothing in jsoncomparison currently
    instantiates but which is exported as part of the package's public
    error hierarchy (unisi/jsoncomparison/__init__.py)."""

    def test_values_not_equal(self):
        err = ValuesNotEqual(1, 2)
        assert err.message == "Values not equal. Expected: <1>, received: <2>"
        assert err.explain() == {
            "_message": "Values not equal. Expected: <1>, received: <2>",
            "_expected": 1,
            "_received": 2,
        }

    def test_types_not_equal_converts_to_type_names(self):
        err = TypesNotEqual("a string", 5)
        assert err.expected == "str"
        assert err.received == "int"
        assert err.message == "Types not equal. Expected: <str>, received: <int>"

    def test_key_not_exist(self):
        err = KeyNotExist("missing_key", None)
        assert err.message == "Key does not exist. Expected: <missing_key>"

    def test_unexpected_key(self):
        err = UnexpectedKey(None, "surprise_key")
        assert err.message == "Unexpected key. Received: <surprise_key>"

    def test_lengths_not_equal(self):
        err = LengthsNotEqual(3, 2)
        assert err.message == "Lengths not equal. Expected <3>, received: <2>"

    def test_value_not_found(self):
        err = ValueNotFound("needle", None)
        assert err.message == "Value not found. Expected <needle>"
        assert err.explain()["_expected"] == "needle"


class TestCompareReport:
    """Compare.report() (console/file output), driven through .check(),
    including regression coverage for the shared-mutable-DEFAULT_CONFIG bug
    and the destructive-pop-in-_write_to_file bug -- see the module
    docstring for the full story on both."""

    def test_console_output_disabled_by_default(self, capsys):
        c = Compare()
        c.check({"a": 1}, {"a": 2})
        assert capsys.readouterr().out == ""

    def test_console_output_writes_the_diff_as_json(self, capsys):
        c = Compare(config={"output": {"console": True, "file": {"name": None}}})
        diff = c.check({"a": 1}, {"a": 2})
        printed = capsys.readouterr().out
        assert printed.strip()
        assert json.loads(printed) == diff

    def test_file_output_disabled_by_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Compare().check({"a": 1}, {"a": 2})
        assert list(tmp_path.iterdir()) == []

    def test_file_output_writes_the_diff_as_json(self, tmp_path):
        target = tmp_path / "out.json"
        c = Compare(config={"output": {"file": {"name": str(target)}}})
        diff = c.check({"a": 1}, {"a": 2})
        assert target.exists()
        assert json.loads(target.read_text()) == diff

    def test_repeated_check_on_the_same_instance_writes_the_file_every_time(self, tmp_path):
        """Regression test: _write_to_file used to pop 'name' straight out of
        the live config dict, so after the FIRST successful write, 'name'
        was gone and every subsequent .check() on this same instance
        silently stopped writing to file at all (no error -- report() just
        decided there was nothing to do)."""
        target = tmp_path / "out.json"
        c = Compare(config={"output": {"file": {"name": str(target)}}})

        c.check({"a": 1}, {"a": 2})
        assert target.exists()
        first_mtime = target.stat().st_mtime_ns
        target.unlink()

        c.check({"a": 1}, {"a": 3})
        assert target.exists(), "second .check() on the same Compare instance must still write the file"
        assert json.loads(target.read_text())["a"]["_received"] == 3

    def test_two_default_instances_do_not_share_config_state(self):
        """Regression test: Compare() with no explicit config used to reuse
        the literal module-level DEFAULT_CONFIG object for every instance
        (Config never copied what it was given, either) -- mutating one
        instance's config was indistinguishable from mutating the process-
        wide default for every other Compare() ever created afterwards."""
        c1 = Compare()
        c2 = Compare()
        assert c1._config.config is not DEFAULT_CONFIG
        assert c1._config.config is not c2._config.config

        c1._config.config["output"]["file"]["name"] = "leaked.json"
        assert c2._config.get("output.file.name") is None
        assert DEFAULT_CONFIG["output"]["file"]["name"] is None

    def test_a_caller_supplied_config_dict_is_not_mutated(self, tmp_path):
        """The deepcopy fix also protects a caller's own config dict from
        being mutated by Compare's internals (the destructive pop used to
        reach into whatever dict was passed in, not just DEFAULT_CONFIG)."""
        target = tmp_path / "out.json"
        my_config = {"output": {"file": {"name": str(target)}}}
        original = copy.deepcopy(my_config)

        Compare(config=my_config).check({"a": 1}, {"a": 2})

        assert my_config == original
