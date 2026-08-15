# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/llmrag.py.

Covers the module end to end: textual and JSON-Schema type conversion,
response shape validation, JSON-comment stripping, prompt building, the
_call_llm retry-on-400 recovery logic, response parsing, the on-disk query
cache, the `images` input feature, setup_llmrag()'s config parsing, and the
public Q() / Qx() / get_property() entry points.

Everything runs against FakeLLMClient (see conftest.py) — no network access
or API key required.

Run with:
    pip install pytest
    pytest tests/unit/test_llmrag.py -v
"""
import base64
import json
import os
import time
from typing import Optional, TypedDict, Union

import pytest

import unisi.llmrag as llmrag
from unisi.common import Unishare

from conftest import make_bad_request_error, run_async


# A real, minimal 1x1 transparent PNG — used wherever "real" image bytes
# are needed (base64 round-tripping, MIME sniffing from content, ...).
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    '+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


# =============================================================================
# _log
# =============================================================================

class TestLog:
    def test_falls_back_to_print_when_no_logger_configured(self, capsys):
        Unishare.message_logger = None
        llmrag._log('something went wrong')
        out = capsys.readouterr().out
        assert '[error] something went wrong' in out

    def test_falls_back_to_print_with_custom_type(self, capsys):
        Unishare.message_logger = None
        llmrag._log('heads up', type='warning')
        out = capsys.readouterr().out
        assert '[warning] heads up' in out

    def test_routes_through_message_logger_when_callable(self, capsys):
        received = []
        Unishare.message_logger = lambda message, type: received.append((message, type))
        llmrag._log('routed message', type='info')
        assert received == [('routed message', 'info')]
        assert capsys.readouterr().out == ''  # did NOT also print


# =============================================================================
# python_type_to_json_schema / jstype — textual schema description
# =============================================================================

class TestPythonTypeToJsonSchema:
    def test_builtin_types(self):
        assert llmrag.python_type_to_json_schema(int) == 'integer'
        assert llmrag.python_type_to_json_schema(float) == 'number'
        assert llmrag.python_type_to_json_schema(str) == 'string'
        assert llmrag.python_type_to_json_schema(dict) == 'object'
        assert llmrag.python_type_to_json_schema(list) == 'array'

    def test_list_of_type(self):
        assert llmrag.python_type_to_json_schema(list[str]) == 'array of string '

    def test_dict_of_types(self):
        assert (
            llmrag.python_type_to_json_schema(dict[str, int])
            == 'object of string to integer structure.'
        )

    def test_dict_generic_with_wrong_arg_count_falls_back_to_plain_object(self):
        # Same malformed-but-constructible generic alias case as
        # TestTypeToSchemaDict's equivalent test, here for the textual
        # description function: get_origin is dict but len(args) != 2.
        assert llmrag.python_type_to_json_schema(dict[int]) == 'object'

    def test_bare_list_and_dict_unaffected_by_the_generic_alias_fix(self):
        # Regression guard: moving the origin check out from under
        # `isinstance(type_value, type)` must not change bare (unsubscripted)
        # list/dict, which are handled earlier via _BUILTIN_TYPE_NAMES.
        assert llmrag.python_type_to_json_schema(list) == 'array'
        assert llmrag.python_type_to_json_schema(dict) == 'object'

    def test_value_instances_unaffected_by_the_generic_alias_fix(self):
        # Regression guard: get_origin() is now called unconditionally,
        # including on plain value instances (42, 'hello', ...) — must
        # still safely fall through to the match-case block for these,
        # since get_origin() returns None for anything that isn't a
        # generic alias rather than raising.
        assert llmrag.python_type_to_json_schema(42) == 'integer'
        assert llmrag.python_type_to_json_schema('hello') == 'string'
        assert llmrag.python_type_to_json_schema([1, 2, 3]) == 'array'
        assert llmrag.python_type_to_json_schema({'a': 1}) == 'object with {"a": "[Type: integer]"} structure'

    def test_value_instances(self):
        assert llmrag.python_type_to_json_schema('hello') == 'string'
        assert llmrag.python_type_to_json_schema(42) == 'integer'
        assert llmrag.python_type_to_json_schema(3.14) == 'number'
        assert llmrag.python_type_to_json_schema([]) == 'array'

    def test_dict_instance_describes_each_field(self):
        result = llmrag.python_type_to_json_schema({'name': str, 'age': int})
        assert result == (
            'object with {"name": "[Type: string]", "age": "[Type: integer]"} structure'
        )

    def test_empty_dict_instance(self):
        assert llmrag.python_type_to_json_schema({}) == 'object'

    def test_bool_value_matches_bool_case(self):
        # bool is checked before int in the match block specifically so
        # this holds: bool is a subclass of int, and isinstance-based
        # `case` patterns match subclasses, so a bool-before-int ordering
        # is required for `case bool():` to ever be reachable at all.
        assert llmrag.python_type_to_json_schema(True) == 'boolean'
        assert llmrag.python_type_to_json_schema(False) == 'boolean'

    def test_int_value_still_matches_int_case(self):
        assert llmrag.python_type_to_json_schema(1) == 'integer'
        assert llmrag.python_type_to_json_schema(0) == 'integer'

    def test_unknown_type_falls_back_to_string(self):
        assert llmrag.python_type_to_json_schema(object()) == 'string'

    def test_unknown_class_falls_back_to_string(self):
        # A `type` that isn't in _BUILTIN_TYPE_NAMES and has no list/dict
        # origin (e.g. a custom class, or `object` itself) — the OTHER
        # fallback, reached via `isinstance(type_value, type)` rather than
        # the match-case block above (that one's for value instances).
        class Custom:
            pass

        assert llmrag.python_type_to_json_schema(Custom) == 'string'
        assert llmrag.python_type_to_json_schema(object) == 'string'

    def test_jstype_alias(self):
        assert llmrag.jstype is llmrag.python_type_to_json_schema


# =============================================================================
# python_type_to_json_schema_dict / _type_to_schema_dict — real JSON Schema
# =============================================================================

class TestTypeToSchemaDict:
    def test_str_returns_none_at_top_level(self):
        assert llmrag.python_type_to_json_schema_dict(str) is None

    def test_date_sentinel_returns_none_at_top_level(self):
        assert llmrag.python_type_to_json_schema_dict('date') is None

    def test_str_inside_a_container_is_a_real_schema(self):
        # Only the TOP-LEVEL str is the "no schema" sentinel; nested str
        # (list[str], a dict-literal field, ...) is a real {'type': 'string'}.
        assert llmrag.python_type_to_json_schema_dict(list[str]) == {
            'type': 'array', 'items': {'type': 'string'},
        }

    def test_builtin_scalars(self):
        assert llmrag.python_type_to_json_schema_dict(int) == {'type': 'integer'}
        assert llmrag.python_type_to_json_schema_dict(float) == {'type': 'number'}
        assert llmrag.python_type_to_json_schema_dict(bool) == {'type': 'boolean'}

    def test_dict_of_types(self):
        assert llmrag.python_type_to_json_schema_dict(dict[str, int]) == {
            'type': 'object', 'additionalProperties': {'type': 'integer'},
        }

    def test_dict_without_type_args(self):
        assert llmrag.python_type_to_json_schema_dict(dict) == {'type': 'object'}

    def test_dict_literal_schema(self):
        schema = llmrag.python_type_to_json_schema_dict(dict(age=int, city=str))
        assert schema == {
            'type': 'object',
            'properties': {'age': {'type': 'integer'}, 'city': {'type': 'string'}},
            'required': ['age', 'city'],
            'additionalProperties': False,
        }

    def test_empty_dict_literal_falls_back_to_string(self):
        # {} is falsy, so `isinstance(type_value, dict) and type_value` is
        # False for it — it falls through to the conservative fallback
        # rather than becoming an (equally reasonable) open {'type': 'object'}.
        assert llmrag.python_type_to_json_schema_dict({}) == {'type': 'string'}

    def test_list_literal_of_type(self):
        assert llmrag.python_type_to_json_schema_dict([str]) == {
            'type': 'array', 'items': {'type': 'string'},
        }

    def test_list_literal_of_objects(self):
        assert llmrag.python_type_to_json_schema_dict([{'a': str}]) == {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {'a': {'type': 'string'}},
                'required': ['a'],
                'additionalProperties': False,
            },
        }

    def test_empty_list_literal_has_no_items_key(self):
        assert llmrag.python_type_to_json_schema_dict([]) == {'type': 'array'}

    def test_optional_unwraps_to_the_inner_type_legacy_syntax(self):
        # typing.Optional[X] / typing.Union[X, None] — the pre-3.10 spelling.
        assert llmrag.python_type_to_json_schema_dict(Optional[int]) == {'type': 'integer'}
        assert llmrag.python_type_to_json_schema_dict(Union[int, None]) == {'type': 'integer'}

    def test_optional_unwraps_to_the_inner_type_pep604_syntax(self):
        # Modern `X | None` syntax — get_origin(int | None) is
        # types.UnionType, a different object from typing.Union used for
        # the legacy spelling above; _is_union_origin() recognises both.
        assert llmrag.python_type_to_json_schema_dict(int | None) == {'type': 'integer'}
        assert llmrag.python_type_to_json_schema_dict(str | None) == {'type': 'string'}
        assert llmrag.python_type_to_json_schema_dict(bool | None) == {'type': 'boolean'}

    def test_complex_union_falls_back_to_string_both_syntaxes(self):
        # 2+ non-None members: deliberately not unwrapped to a precise
        # type (documented in the source as "can't express precisely, but
        # not None either") — same {'type': 'string'} fallback via the
        # SAME Union-handling branch for both spellings now.
        assert llmrag.python_type_to_json_schema_dict(Union[int, str]) == {'type': 'string'}
        assert llmrag.python_type_to_json_schema_dict(int | str) == {'type': 'string'}

    def test_dict_generic_with_wrong_arg_count_omits_additional_properties(self):
        # dict[int] is a malformed-but-constructible generic alias (runtime
        # doesn't enforce dict's usual 2-arg shape) — get_origin is dict but
        # args has length 1, missing the `len(args) == 2` guard, landing on
        # the plain {'type': 'object'} fallback rather than one with
        # additionalProperties.
        assert llmrag.python_type_to_json_schema_dict(dict[int]) == {'type': 'object'}

    def test_typed_dict(self):
        class Person(TypedDict):
            name: str
            age: int

        schema = llmrag.python_type_to_json_schema_dict(Person)
        assert schema == {
            'type': 'object',
            'properties': {'name': {'type': 'string'}, 'age': {'type': 'integer'}},
            'required': ['age', 'name'],
            'additionalProperties': False,
        }

    def test_typed_dict_total_false_has_nothing_required(self):
        class Movie(TypedDict, total=False):
            title: str

        assert llmrag.python_type_to_json_schema_dict(Movie)['required'] == []

    def test_nested_typed_dict_field(self):
        class Address(TypedDict):
            city: str

        class Person(TypedDict):
            name: str
            address: Address

        schema = llmrag.python_type_to_json_schema_dict(Person)
        assert schema['properties']['address'] == {
            'type': 'object',
            'properties': {'city': {'type': 'string'}},
            'required': ['city'],
            'additionalProperties': False,
        }

    def test_unknown_type_falls_back_to_string(self):
        assert llmrag.python_type_to_json_schema_dict(object) == {'type': 'string'}


# =============================================================================
# _type_key — stable cache-key component for a type/schema
# =============================================================================

class TestTypeKey:
    def test_field_order_does_not_matter(self):
        assert llmrag._type_key(dict(age=int, city=str)) == llmrag._type_key(dict(city=str, age=int))

    def test_different_shapes_differ(self):
        assert llmrag._type_key(int) != llmrag._type_key(list[str])

    def test_is_valid_json(self):
        json.loads(llmrag._type_key(dict(name=str)))  # must not raise


# =============================================================================
# _validate_against_type
# =============================================================================

class TestValidateAgainstType:
    def test_passes_for_matching_scalars(self):
        llmrag._validate_against_type('hello', str)
        llmrag._validate_against_type(5, int)
        llmrag._validate_against_type(3.5, float)

    def test_raises_for_mismatched_scalar(self):
        with pytest.raises(ValueError, match=r'\$: expected string, got int'):
            llmrag._validate_against_type(5, str)

    def test_bool_rejected_where_int_expected(self):
        # isinstance(True, int) is True in Python (bool subclasses int),
        # which used to let a JSON true/false value silently satisfy an
        # `int`-typed field. Now excluded explicitly — see
        # test_int_rejected_where_bool_expected for the direction that was
        # always correct (isinstance(1, bool) was already False).
        with pytest.raises(ValueError, match=r'\$: expected int, got bool'):
            llmrag._validate_against_type(True, int)

    def test_int_rejected_where_bool_expected(self):
        with pytest.raises(ValueError, match=r'\$: expected bool, got int'):
            llmrag._validate_against_type(1, bool)

    def test_bool_still_accepted_where_bool_expected(self):
        # Regression guard for the fix above: only `t is int` excludes
        # bool now — `t is bool` must still accept True/False normally.
        llmrag._validate_against_type(True, bool)
        llmrag._validate_against_type(False, bool)

    def test_float_still_rejects_bool(self):
        # Unaffected by the fix — isinstance(True, float) was already
        # False, this direction was never broken.
        with pytest.raises(ValueError, match=r'\$: expected float, got bool'):
            llmrag._validate_against_type(True, float)

    def test_date_sentinel_requires_a_string(self):
        llmrag._validate_against_type('01/02/2026', 'date')
        with pytest.raises(ValueError):
            llmrag._validate_against_type(20260102, 'date')

    def test_dict_literal_missing_required_field(self):
        with pytest.raises(ValueError, match=r"\$: missing required field 'age'"):
            llmrag._validate_against_type({'city': 'Bangkok'}, dict(age=int, city=str))

    def test_dict_literal_wrong_field_type_reports_path(self):
        with pytest.raises(ValueError, match=r'\$\.age: expected int, got str'):
            llmrag._validate_against_type({'age': 'old', 'city': 'x'}, dict(age=int, city=str))

    def test_dict_literal_valid(self):
        llmrag._validate_against_type({'age': 30, 'city': 'x'}, dict(age=int, city=str))

    def test_list_of_type_wrong_item_reports_index(self):
        with pytest.raises(ValueError, match=r'\$\[1\]: expected string, got int'):
            llmrag._validate_against_type(['a', 2, 'c'], list[str])

    def test_dict_of_types_validates_values(self):
        llmrag._validate_against_type({'a': 1, 'b': 2}, dict[str, int])
        with pytest.raises(ValueError):
            llmrag._validate_against_type({'a': 'x'}, dict[str, int])

    def test_typed_dict_missing_required_key(self):
        class Person(TypedDict):
            name: str
            age: int

        with pytest.raises(ValueError, match=r"missing required field 'age'"):
            llmrag._validate_against_type({'name': 'Ann'}, Person)

    def test_typed_dict_rejects_a_non_dict_value(self):
        class Person(TypedDict):
            name: str

        with pytest.raises(ValueError, match=r'expected object \(TypedDict Person\), got str'):
            llmrag._validate_against_type('not a dict', Person)

    def test_typed_dict_valid(self):
        class Person(TypedDict):
            name: str
            age: int

        llmrag._validate_against_type({'name': 'Ann', 'age': 30}, Person)

    def test_list_of_type_rejects_a_non_list_value(self):
        with pytest.raises(ValueError, match=r'\$: expected list, got str'):
            llmrag._validate_against_type('not a list', list[str])

    def test_dict_of_types_rejects_a_non_dict_value(self):
        with pytest.raises(ValueError, match=r'\$: expected dict, got list'):
            llmrag._validate_against_type([1, 2], dict[str, int])

    def test_list_literal_schema_rejects_a_non_list_value(self):
        with pytest.raises(ValueError, match=r'\$: expected list \(array\), got str'):
            llmrag._validate_against_type('not a list', [str])

    def test_dict_literal_schema_rejects_a_non_dict_value(self):
        with pytest.raises(ValueError, match=r'\$: expected object, got list'):
            llmrag._validate_against_type([1, 2], dict(age=int))

    def test_complex_union_legacy_syntax_is_not_strictly_checked(self):
        # typing.Union[int, str] (2+ non-None members): the code comment
        # calls this out explicitly — deliberately permissive, not a bug.
        llmrag._validate_against_type('anything at all', Union[int, str])
        llmrag._validate_against_type(12345, Union[int, str])
        llmrag._validate_against_type(None, Union[int, str])

    def test_complex_union_pep604_syntax_is_not_strictly_checked_too(self):
        # Same deliberate permissiveness, now also reachable via the
        # modern spelling now that both origins are recognised.
        llmrag._validate_against_type('anything at all', int | str)
        llmrag._validate_against_type(12345, int | str)

    def test_optional_legacy_syntax_allows_none_or_value(self):
        llmrag._validate_against_type(None, Optional[str])
        llmrag._validate_against_type('x', Optional[str])

    def test_optional_legacy_syntax_rejects_wrong_type(self):
        with pytest.raises(ValueError):
            llmrag._validate_against_type(12345, Optional[str])

    def test_optional_pep604_syntax_allows_none_or_value(self):
        # Modern `X | None` spelling — get_origin(str | None) is
        # types.UnionType rather than typing.Union, but _is_union_origin()
        # recognises both, so this now behaves identically to
        # test_optional_legacy_syntax_allows_none_or_value above.
        llmrag._validate_against_type(None, str | None)
        llmrag._validate_against_type('x', str | None)

    def test_optional_pep604_syntax_rejects_wrong_type(self):
        with pytest.raises(ValueError, match=r'\$: expected string, got int'):
            llmrag._validate_against_type(12345, str | None)
        with pytest.raises(ValueError):
            llmrag._validate_against_type([1, 2, 3], str | None)

    def test_list_literal_schema(self):
        llmrag._validate_against_type(['a', 'b'], [str])
        with pytest.raises(ValueError):
            llmrag._validate_against_type([1, 2], [str])


# =============================================================================
# remove_json_comments / remove_comments
# =============================================================================

class TestRemoveJsonComments:
    def test_strips_line_comment(self):
        raw = '{\n  "a": 1, // comment\n  "b": 2\n}'
        assert json.loads(llmrag.remove_json_comments(raw)) == {'a': 1, 'b': 2}

    def test_strips_block_comment(self):
        raw = '{ "a": 1, /* comment */ "b": 2 }'
        assert json.loads(llmrag.remove_json_comments(raw)) == {'a': 1, 'b': 2}

    def test_line_comment_marker_inside_string_is_preserved(self):
        raw = '{"url": "https://example.com"}'
        assert json.loads(llmrag.remove_json_comments(raw)) == {'url': 'https://example.com'}

    def test_block_comment_markers_inside_string_are_preserved(self):
        raw = '{"note": "/* not a comment */"}'
        assert json.loads(llmrag.remove_json_comments(raw)) == {'note': '/* not a comment */'}

    def test_escaped_quotes_do_not_end_the_string_early(self):
        raw = r'{"quote": "she said \"hi // there\""}'
        assert json.loads(llmrag.remove_json_comments(raw)) == {'quote': 'she said "hi // there"'}

    def test_unterminated_block_comment_consumes_to_end(self):
        # Must not crash even if the LLM emits a truncated/malformed comment.
        raw = '{"a": 1} /* unterminated'
        cleaned = llmrag.remove_json_comments(raw)
        assert json.loads(cleaned) == {'a': 1}

    def test_no_comments_unchanged(self):
        assert json.loads(llmrag.remove_json_comments('{"a": 1}')) == {'a': 1}

    def test_remove_comments_alias(self):
        assert llmrag.remove_comments is llmrag.remove_json_comments


# =============================================================================
# _safe_format
# =============================================================================

class TestSafeFormat:
    def test_substitutes_known_keys(self):
        assert llmrag._safe_format('Capital of {country}?', {'country': 'France'}) == 'Capital of France?'

    def test_leaves_unknown_braces_untouched(self):
        prompt = 'Answer as {"key": "value"}. Name: {name}'
        assert llmrag._safe_format(prompt, {'name': 'Ann'}) == 'Answer as {"key": "value"}. Name: Ann'

    def test_empty_format_vars_returns_prompt_unchanged(self):
        assert llmrag._safe_format('Hello {name}', {}) == 'Hello {name}'

    def test_repeated_placeholder_all_substituted(self):
        assert llmrag._safe_format('{x} and {x}', {'x': 'A'}) == 'A and A'

    def test_non_string_value_is_stringified(self):
        assert llmrag._safe_format('Count: {n}', {'n': 5}) == 'Count: 5'


# =============================================================================
# _build_prompt
# =============================================================================

class TestBuildPrompt:
    def test_extend_false_only_substitutes(self):
        result = llmrag._build_prompt(
            'Hi {name}', str, extend=False, identity='ID.', format_vars={'name': 'Ann'}
        )
        assert result == 'Hi Ann'

    def test_extend_true_str_type_prepends_identity_only(self):
        result = llmrag._build_prompt('Question?', str, extend=True, identity='ID.', format_vars={})
        assert result == 'ID.Question?'

    def test_extend_true_structured_type_adds_instruction(self):
        result = llmrag._build_prompt(
            'Question?', dict(a=int), extend=True, identity='ID.', format_vars={}
        )
        assert result == 'ID. DO NOT OUTPUT ANY COMMENTARY. Output only the requested data.Question?'

    def test_extend_true_date_type_adds_date_instruction(self):
        result = llmrag._build_prompt('Birthday?', 'date', extend=True, identity='ID.', format_vars={})
        assert result == 'ID. Output STRONGLY in format dd/mm/yyyy string. DO NOT OUTPUT ANY COMMENTARY.Birthday?'

    def test_extend_true_type_none_leaves_prompt_unchanged(self):
        result = llmrag._build_prompt('Question?', None, extend=True, identity='ID.', format_vars={})
        assert result == 'Question?'

    def test_format_vars_applied_before_identity_prefix(self):
        result = llmrag._build_prompt(
            'About {topic}', str, extend=True, identity='ID.', format_vars={'topic': 'cats'}
        )
        assert result == 'ID.About cats'


# =============================================================================
# _rejected_param
# =============================================================================

class TestRejectedParam:
    def test_unsupported_value_code(self):
        exc = make_bad_request_error(param='temperature', code='unsupported_value')
        assert llmrag._rejected_param(exc) == 'temperature'

    def test_unsupported_parameter_code(self):
        exc = make_bad_request_error(param='response_format', code='unsupported_parameter')
        assert llmrag._rejected_param(exc) == 'response_format'

    def test_message_mentions_support_even_with_other_code(self):
        exc = make_bad_request_error(param='strict', code='invalid_request_error',
                                      message="'strict' is not supported for this model")
        assert llmrag._rejected_param(exc) == 'strict'

    def test_param_present_but_not_an_unsupported_error_returns_none(self):
        exc = make_bad_request_error(param='messages', code='invalid_request_error',
                                      message='messages is a required property')
        assert llmrag._rejected_param(exc) is None

    def test_no_param_returns_none(self):
        exc = make_bad_request_error(param=None, code='invalid_request_error', message='bad request')
        assert llmrag._rejected_param(exc) is None


# =============================================================================
# Image input normalisation (images= on Q()/Qx())
# =============================================================================

class TestSniffImageMime:
    def test_png(self):
        assert llmrag._sniff_image_mime(PNG_BYTES) == 'image/png'

    def test_jpeg(self):
        assert llmrag._sniff_image_mime(b'\xff\xd8\xffrest-of-file') == 'image/jpeg'

    def test_gif87a(self):
        assert llmrag._sniff_image_mime(b'GIF87a...') == 'image/gif'

    def test_gif89a(self):
        assert llmrag._sniff_image_mime(b'GIF89a...') == 'image/gif'

    def test_bmp(self):
        assert llmrag._sniff_image_mime(b'BM....') == 'image/bmp'

    def test_webp_riff_container(self):
        webp = b'RIFF' + b'\x00\x00\x00\x00' + b'WEBPVP8 ...'
        assert llmrag._sniff_image_mime(webp) == 'image/webp'

    def test_riff_without_webp_marker_is_not_misdetected(self):
        wav = b'RIFF' + b'\x00\x00\x00\x00' + b'WAVEfmt '
        assert llmrag._sniff_image_mime(wav) != 'image/webp'

    def test_unknown_bytes_fall_back_to_png(self):
        assert llmrag._sniff_image_mime(b'not-an-image') == 'image/png'

    def test_custom_fallback(self):
        assert llmrag._sniff_image_mime(b'???', fallback='image/x-custom') == 'image/x-custom'


class TestBytesAndFileToDataUri:
    def test_bytes_round_trip(self):
        uri = llmrag._bytes_to_data_uri(PNG_BYTES)
        assert uri.startswith('data:image/png;base64,')
        assert base64.b64decode(uri.split(',', 1)[1]) == PNG_BYTES

    def test_bytes_with_explicit_mime(self):
        uri = llmrag._bytes_to_data_uri(b'whatever', mime='image/jpeg')
        assert uri.startswith('data:image/jpeg;base64,')

    def test_bytearray_accepted(self):
        uri = llmrag._bytes_to_data_uri(bytearray(PNG_BYTES))
        assert uri.startswith('data:image/png;base64,')

    def test_file_round_trip(self, tmp_path):
        path = tmp_path / 'photo.png'
        path.write_bytes(PNG_BYTES)
        uri = llmrag._file_to_data_uri(str(path))
        assert uri.startswith('data:image/png;base64,')
        assert base64.b64decode(uri.split(',', 1)[1]) == PNG_BYTES

    def test_file_mime_from_extension(self, tmp_path):
        # .jpg extension should be honoured even though the bytes are a PNG.
        path = tmp_path / 'photo.jpg'
        path.write_bytes(PNG_BYTES)
        uri = llmrag._file_to_data_uri(str(path))
        assert uri.startswith('data:image/jpeg;base64,')

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            llmrag._file_to_data_uri(str(tmp_path / 'nope.png'))


class TestImageContentPart:
    def test_http_url_passthrough(self):
        part = llmrag._image_content_part('http://example.com/cat.png')
        assert part == {'type': 'image_url', 'image_url': {'url': 'http://example.com/cat.png'}}

    def test_https_url_passthrough(self):
        part = llmrag._image_content_part('https://example.com/cat.png')
        assert part['image_url']['url'] == 'https://example.com/cat.png'

    def test_data_uri_passthrough(self):
        data_uri = 'data:image/png;base64,AAAA'
        part = llmrag._image_content_part(data_uri)
        assert part['image_url']['url'] == data_uri

    def test_plain_string_treated_as_local_path(self, tmp_path):
        path = tmp_path / 'photo.png'
        path.write_bytes(PNG_BYTES)
        part = llmrag._image_content_part(str(path))
        assert part['image_url']['url'].startswith('data:image/png;base64,')

    def test_bytes_encoded(self):
        part = llmrag._image_content_part(PNG_BYTES)
        assert part['type'] == 'image_url'
        assert part['image_url']['url'].startswith('data:image/png;base64,')

    def test_dict_with_url(self):
        part = llmrag._image_content_part({'url': 'https://example.com/a.png'})
        assert part == {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}}

    def test_dict_with_url_and_detail(self):
        part = llmrag._image_content_part({'url': 'https://example.com/a.png', 'detail': 'low'})
        assert part['image_url'] == {'url': 'https://example.com/a.png', 'detail': 'low'}

    def test_dict_with_path(self, tmp_path):
        path = tmp_path / 'photo.png'
        path.write_bytes(PNG_BYTES)
        part = llmrag._image_content_part({'path': str(path)})
        assert part['image_url']['url'].startswith('data:image/png;base64,')

    def test_dict_with_data_and_mime(self):
        part = llmrag._image_content_part({'data': PNG_BYTES, 'mime': 'image/png'})
        assert part['image_url']['url'].startswith('data:image/png;base64,')

    def test_dict_prebuilt_content_part_passthrough(self):
        prebuilt = {'type': 'image_url', 'image_url': {'url': 'https://example.com/c.png', 'detail': 'high'}}
        assert llmrag._image_content_part(prebuilt) is prebuilt

    def test_dict_missing_required_keys_raises(self):
        with pytest.raises(ValueError):
            llmrag._image_content_part({'nope': 1})

    def test_unsupported_type_raises_type_error(self):
        with pytest.raises(TypeError):
            llmrag._image_content_part(12345)


class TestBuildMessageContent:
    def test_no_images_returns_plain_string(self):
        content = llmrag._build_message_content('hello', None)
        assert content == 'hello'
        assert isinstance(content, str)

    def test_empty_list_returns_plain_string(self):
        assert llmrag._build_message_content('hello', []) == 'hello'

    def test_single_image_produces_text_and_image_parts(self):
        content = llmrag._build_message_content('describe', 'https://example.com/a.png')
        assert content == [
            {'type': 'text', 'text': 'describe'},
            {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}},
        ]

    def test_multiple_images_preserve_order(self):
        content = llmrag._build_message_content('compare', ['https://a.png', 'https://b.png'])
        urls = [part['image_url']['url'] for part in content[1:]]
        assert urls == ['https://a.png', 'https://b.png']

    def test_none_entries_in_a_list_are_skipped(self):
        content = llmrag._build_message_content('x', ['https://a.png', None])
        assert len(content) == 2  # text + one image, the None was skipped


class TestImagesCacheKey:
    def test_same_url_same_key(self):
        assert llmrag._images_cache_key('https://a.png') == llmrag._images_cache_key('https://a.png')

    def test_different_url_different_key(self):
        assert llmrag._images_cache_key('https://a.png') != llmrag._images_cache_key('https://b.png')

    def test_same_bytes_same_key(self):
        assert llmrag._images_cache_key(PNG_BYTES) == llmrag._images_cache_key(bytes(PNG_BYTES))

    def test_different_bytes_different_key(self):
        assert llmrag._images_cache_key(PNG_BYTES) != llmrag._images_cache_key(PNG_BYTES + b'\x00')

    def test_bytes_are_hashed_not_inlined(self):
        key = llmrag._images_cache_key(PNG_BYTES)
        assert base64.b64encode(PNG_BYTES).decode() not in key

    def test_long_string_is_hashed(self):
        long_data_uri = 'data:image/png;base64,' + 'A' * 500
        key = llmrag._images_cache_key(long_data_uri)
        assert long_data_uri not in key
        assert len(key) < len(long_data_uri)

    def test_short_string_kept_readable(self):
        assert llmrag._images_cache_key('https://a.png') == 'https://a.png'

    def test_list_order_affects_key(self):
        # Documents current behaviour: the key is order-sensitive
        # (join in list order), so [a, b] and [b, a] produce different
        # keys even though they're "the same two images".
        assert (
            llmrag._images_cache_key(['https://a.png', 'https://b.png'])
            != llmrag._images_cache_key(['https://b.png', 'https://a.png'])
        )

    def test_dict_form_same_url_same_key(self):
        assert (
            llmrag._images_cache_key({'url': 'https://a.png', 'detail': 'low'})
            == llmrag._images_cache_key({'url': 'https://a.png', 'detail': 'low'})
        )

    def test_dict_form_different_detail_different_key(self):
        assert (
            llmrag._images_cache_key({'url': 'https://a.png', 'detail': 'low'})
            != llmrag._images_cache_key({'url': 'https://a.png', 'detail': 'high'})
        )

    def test_dict_with_raw_bytes_data_is_hashed_not_inlined(self):
        key = llmrag._images_cache_key({'data': PNG_BYTES, 'mime': 'image/png'})
        assert base64.b64encode(PNG_BYTES).decode() not in key
        assert 'image/png' in key  # non-bytes fields stay human-readable

    def test_dict_with_same_bytes_data_same_key(self):
        assert (
            llmrag._images_cache_key({'data': PNG_BYTES, 'mime': 'image/png'})
            == llmrag._images_cache_key({'data': bytes(PNG_BYTES), 'mime': 'image/png'})
        )

    def test_dict_with_different_bytes_data_different_key(self):
        assert (
            llmrag._images_cache_key({'data': PNG_BYTES})
            != llmrag._images_cache_key({'data': PNG_BYTES + b'\x00'})
        )


# =============================================================================
# _call_llm
# =============================================================================

class TestCallLLM:
    @run_async
    async def test_raises_when_not_initialised(self):
        llmrag._acompletion = None
        with pytest.raises(RuntimeError, match='LLM not initialised'):
            await llmrag._call_llm('hello')

    @run_async
    async def test_str_type_sends_no_response_format(self, fake_llm):
        await llmrag._call_llm('hello', str)
        assert 'response_format' not in fake_llm.calls[0]
        assert fake_llm.calls[0]['messages'] == [{'role': 'user', 'content': 'hello'}]
        assert fake_llm.calls[0]['model'] == 'test-model'

    @run_async
    async def test_structured_type_sends_schema_and_strict_flag(self, fake_llm):
        await llmrag._call_llm('give me a person', dict(name=str))
        rf = fake_llm.calls[0]['response_format']
        assert rf['type'] == 'json_schema'
        assert rf['json_schema']['schema'] == {
            'type': 'object',
            'properties': {'name': {'type': 'string'}},
            'required': ['name'],
            'additionalProperties': False,
        }
        assert rf['json_schema']['strict'] is True  # Unishare.llm_strict_schema default

    @run_async
    async def test_temperature_included_by_default(self, fake_llm):
        await llmrag._call_llm('hello', str)
        assert fake_llm.calls[0]['temperature'] == 0.3  # set by the fake_llm fixture

    @run_async
    async def test_temperature_omitted_for_a_model_that_previously_rejected_it(self, fake_llm):
        llmrag._incompatible_params['test-model'] = {'temperature'}
        await llmrag._call_llm('hello', str)
        assert 'temperature' not in fake_llm.calls[0]

    @run_async
    async def test_images_become_multipart_content(self, fake_llm):
        await llmrag._call_llm('describe', str, images='https://example.com/a.png')
        content = fake_llm.calls[0]['messages'][0]['content']
        assert content == [
            {'type': 'text', 'text': 'describe'},
            {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}},
        ]

    @run_async
    async def test_no_images_keeps_content_a_plain_string(self, fake_llm):
        await llmrag._call_llm('describe', str, images=None)
        assert fake_llm.calls[0]['messages'][0]['content'] == 'describe'

    @run_async
    async def test_extra_body_forwarded_when_configured(self, fake_llm):
        Unishare.llm_extra_body = {'reasoning': {'effort': 'high', 'enabled': True}}
        await llmrag._call_llm('hello', str)
        assert fake_llm.calls[0]['extra_body'] == {'reasoning': {'effort': 'high', 'enabled': True}}

    @run_async
    async def test_extra_body_absent_when_not_configured(self, fake_llm):
        await llmrag._call_llm('hello', str)
        assert 'extra_body' not in fake_llm.calls[0]

    @run_async
    async def test_retries_and_learns_when_temperature_is_rejected(self, fake_llm):
        fake_llm.responses = ['final answer']
        fake_llm.errors = {0: make_bad_request_error(param='temperature', code='unsupported_value')}

        result = await llmrag._call_llm('hello', str)

        assert result == 'final answer'
        assert len(fake_llm.calls) == 2
        assert 'temperature' in fake_llm.calls[0]       # first attempt: tried it
        assert 'temperature' not in fake_llm.calls[1]    # retry: dropped it
        assert llmrag._incompatible_params['test-model'] == {'temperature'}  # learned

        # A fresh call for the same model should skip straight past the
        # doomed first attempt — no temperature sent, no error raised.
        fake_llm.calls.clear()
        fake_llm.errors.clear()
        await llmrag._call_llm('again', str)
        assert len(fake_llm.calls) == 1
        assert 'temperature' not in fake_llm.calls[0]

    @run_async
    async def test_retries_and_learns_when_strict_schema_is_rejected(self, fake_llm):
        fake_llm.responses = ['{"name": "Ann"}']
        fake_llm.errors = {0: make_bad_request_error(param='response_format.json_schema.strict',
                                                       code='unsupported_parameter')}

        result = await llmrag._call_llm('hello', dict(name=str))

        assert result == '{"name": "Ann"}'
        assert len(fake_llm.calls) == 2
        assert fake_llm.calls[0]['response_format']['json_schema']['strict'] is True
        assert fake_llm.calls[1]['response_format']['json_schema']['strict'] is False
        assert llmrag._incompatible_params['test-model'] == {'strict'}

    @run_async
    async def test_both_temperature_and_strict_rejected_recovers_on_third_attempt(self, fake_llm):
        fake_llm.responses = ['{"name": "Ann"}']
        fake_llm.errors = {
            0: make_bad_request_error(param='temperature', code='unsupported_value'),
            1: make_bad_request_error(param='response_format', code='unsupported_parameter'),
        }

        result = await llmrag._call_llm('hello', dict(name=str))

        assert result == '{"name": "Ann"}'
        assert len(fake_llm.calls) == 3
        assert 'temperature' not in fake_llm.calls[2]
        assert fake_llm.calls[2]['response_format']['json_schema']['strict'] is False
        assert llmrag._incompatible_params['test-model'] == {'temperature', 'strict'}

    @run_async
    async def test_non_recoverable_bad_request_propagates_immediately(self, fake_llm):
        fake_llm.errors = {0: make_bad_request_error(param='messages', code='invalid_request_error',
                                                       message='messages is required')}
        with pytest.raises(llmrag._BadRequestError):
            await llmrag._call_llm('hello', str)
        assert len(fake_llm.calls) == 1  # no retry attempted

    @run_async
    async def test_non_bad_request_exception_propagates(self, fake_llm):
        class Boom(Exception):
            pass

        async def raise_boom(**kwargs):
            raise Boom('network is down')

        llmrag._acompletion = raise_boom
        with pytest.raises(Boom):
            await llmrag._call_llm('hello', str)


# =============================================================================
# _parse_response
# =============================================================================

class TestParseResponse:
    def test_str_type_returns_content_as_is(self):
        assert llmrag._parse_response('  hello world  ', str) == 'hello world'

    def test_date_type_returns_content_as_is(self):
        assert llmrag._parse_response('01/02/2026', 'date') == '01/02/2026'

    def test_strips_code_fence_and_json_prefix(self):
        content = '```json\n{"a": 1}\n```'
        assert llmrag._parse_response(content, dict(a=int)) == {'a': 1}

    def test_strips_plain_code_fence(self):
        content = '```\n{"a": 1}\n```'
        assert llmrag._parse_response(content, dict(a=int)) == {'a': 1}

    def test_valid_json_matching_schema(self):
        assert llmrag._parse_response('{"name": "Ann", "age": 30}', dict(name=str, age=int)) == {
            'name': 'Ann', 'age': 30,
        }

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match='Invalid JSON from LLM'):
            llmrag._parse_response('not json at all', dict(a=int))

    def test_valid_json_wrong_shape_raises_value_error(self):
        with pytest.raises(ValueError):
            llmrag._parse_response('{"a": "not a number"}', dict(a=int))

    def test_comments_are_stripped_before_parsing(self):
        content = '{"a": 1 // trailing comment\n}'
        assert llmrag._parse_response(content, dict(a=int)) == {'a': 1}

    def test_list_type(self):
        assert llmrag._parse_response('["a", "b", "c"]', list[str]) == ['a', 'b', 'c']


# =============================================================================
# QueryCache
# =============================================================================

class TestQueryCache:
    def test_set_and_get_round_trip(self, tmp_path):
        cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        cache.set('key1', 'value1')
        assert cache.get('key1') == 'value1'
        cache.close()

    def test_missing_key_returns_none(self, tmp_path):
        cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        assert cache.get('does-not-exist') is None
        cache.close()

    def test_ttl_expiry(self, tmp_path):
        cache = llmrag.QueryCache(str(tmp_path / 'cache'), ttl=0.05)
        cache.set('key1', 'value1')
        assert cache.get('key1') == 'value1'
        time.sleep(0.2)
        assert cache.get('key1') is None
        cache.close()

    def test_no_ttl_does_not_expire_quickly(self, tmp_path):
        cache = llmrag.QueryCache(str(tmp_path / 'cache'), ttl=None)
        cache.set('key1', 'value1')
        time.sleep(0.1)
        assert cache.get('key1') == 'value1'
        cache.close()

    def test_close_does_not_raise(self, tmp_path):
        cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        cache.close()  # just must not raise


# =============================================================================
# Q()
# =============================================================================

class TestQ:
    @run_async
    async def test_returns_llm_content_for_str_type(self, fake_llm):
        fake_llm.responses = ['Paris']
        result = await llmrag.Q('Capital of France?', str)
        assert result == 'Paris'

    @run_async
    async def test_identity_is_prepended_to_the_sent_prompt(self, fake_llm):
        await llmrag.Q('What is Python?', str)
        content = fake_llm.calls[0]['messages'][0]['content']
        assert content.startswith(llmrag._DEFAULT_IDENTITY)
        assert content.endswith('What is Python?')

    @run_async
    async def test_format_vars_are_substituted(self, fake_llm):
        await llmrag.Q('Capital of {country}?', str, country='Thailand')
        content = fake_llm.calls[0]['messages'][0]['content']
        assert 'Capital of Thailand?' in content

    @run_async
    async def test_format_false_leaves_placeholders_untouched(self, fake_llm):
        await llmrag.Q('Capital of {country}?', str, format=False, country='Thailand')
        content = fake_llm.calls[0]['messages'][0]['content']
        assert 'Capital of {country}?' in content

    @run_async
    async def test_structured_response_parsed_and_validated(self, fake_llm):
        fake_llm.responses = ['{"capital": "Bangkok", "population": 10}']
        result = await llmrag.Q('Info about Thailand', dict(capital=str, population=int))
        assert result == {'capital': 'Bangkok', 'population': 10}

    @run_async
    async def test_identity_override_is_used_as_prefix_and_not_left_as_placeholder(self, fake_llm):
        await llmrag.Q('Hello {identity} there', str, identity='Custom persona. ')
        content = fake_llm.calls[0]['messages'][0]['content']
        assert content.startswith('Custom persona. ')
        # 'identity' is popped from format_vars before _safe_format runs, so
        # a LITERAL '{identity}' placeholder in the prompt text is not a
        # substitution target — it stays exactly as written.
        assert '{identity}' in content

    @run_async
    async def test_extend_false_sends_raw_prompt(self, fake_llm):
        await llmrag.Q('Raw prompt', str, extend=False)
        assert fake_llm.calls[0]['messages'][0]['content'] == 'Raw prompt'

    # --- caching -------------------------------------------------------

    @run_async
    async def test_identical_calls_hit_the_cache(self, fake_llm, tmp_path):
        Unishare.llm_cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        r1 = await llmrag.Q('Same prompt', str)
        r2 = await llmrag.Q('Same prompt', str)
        assert r1 == r2
        assert len(fake_llm.calls) == 1  # second call served from cache

    @run_async
    async def test_cache_disabled_by_default_queries_every_time(self, fake_llm):
        await llmrag.Q('Same prompt', str)
        await llmrag.Q('Same prompt', str)
        assert len(fake_llm.calls) == 2  # Unishare.llm_cache is None (fake_llm default)

    @run_async
    async def test_images_produce_a_separate_cache_entry(self, fake_llm, tmp_path):
        Unishare.llm_cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        await llmrag.Q('Same prompt', str)
        await llmrag.Q('Same prompt', str, images=PNG_BYTES)
        assert len(fake_llm.calls) == 2  # different cache key -> second is a real call

        await llmrag.Q('Same prompt', str, images=PNG_BYTES)
        assert len(fake_llm.calls) == 2  # same image -> cache hit, no 3rd call

        await llmrag.Q('Same prompt', str, images=b'\xff\xd8\xff' + b'0' * 20)
        assert len(fake_llm.calls) == 3  # a different image -> cache miss again

    @run_async
    async def test_no_images_cache_key_is_unaffected_by_images_feature(self, fake_llm, tmp_path):
        """
        Backward compatibility: a pre-upgrade cache entry, written under the
        OLD key format (no image awareness at all), must still be found by
        a plain no-images Q() call after upgrading — so shipping `images`
        support does not silently invalidate everyone's existing on-disk
        cache for the common (no-image) case.
        """
        Unishare.llm_cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        prompt = 'Pre-existing cached prompt.'
        final_prompt = llmrag._build_prompt(
            prompt, str, extend=True, identity=llmrag._DEFAULT_IDENTITY, format_vars={}
        )
        old_style_key = f'{llmrag._type_key(str)}:{final_prompt}'
        Unishare.llm_cache.set(old_style_key, 'PRESET-FROM-BEFORE-IMAGES-EXISTED')

        result = await llmrag.Q(prompt, str)

        assert result == 'PRESET-FROM-BEFORE-IMAGES-EXISTED'
        assert len(fake_llm.calls) == 0  # served from the old-format cache entry

    @run_async
    async def test_malformed_cached_entry_is_treated_as_a_miss(self, fake_llm, tmp_path):
        Unishare.llm_cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        final_prompt = llmrag._build_prompt(
            'Q', dict(a=int), extend=True, identity=llmrag._DEFAULT_IDENTITY, format_vars={}
        )
        key = f'{llmrag._type_key(dict(a=int))}:{final_prompt}'
        Unishare.llm_cache.set(key, 'this is not valid json')
        fake_llm.responses = ['{"a": 1}']

        result = await llmrag.Q('Q', dict(a=int))

        assert result == {'a': 1}
        assert len(fake_llm.calls) == 1  # had to go to the LLM, cache entry was unusable

    @run_async
    async def test_malformed_llm_response_is_never_cached(self, fake_llm, tmp_path):
        Unishare.llm_cache = llmrag.QueryCache(str(tmp_path / 'cache'))
        fake_llm.responses = ['not valid json']

        with pytest.raises(ValueError):
            await llmrag.Q('Q', dict(a=int))

        fake_llm.responses = ['not valid json']  # still broken
        with pytest.raises(ValueError):
            await llmrag.Q('Q', dict(a=int))

        # Both calls reached the LLM — the first bad response was never
        # cached, so the second call didn't just replay it from cache
        # (it independently reached the LLM and failed again on its own).
        assert len(fake_llm.calls) == 2


# =============================================================================
# Qx()
# =============================================================================

class TestQx:
    @run_async
    async def test_sends_completely_raw_prompt(self, fake_llm):
        await llmrag.Qx('Raw prompt {not_a_var}', str)
        content = fake_llm.calls[0]['messages'][0]['content']
        assert content == 'Raw prompt {not_a_var}'  # no identity, no substitution

    @run_async
    async def test_returns_llm_content(self, fake_llm):
        fake_llm.responses = ['raw answer']
        assert await llmrag.Qx('Anything', str) == 'raw answer'

    @run_async
    async def test_forwards_images(self, fake_llm):
        await llmrag.Qx('Describe', str, images='https://example.com/x.png')
        content = fake_llm.calls[0]['messages'][0]['content']
        assert content[1]['image_url']['url'] == 'https://example.com/x.png'

    @run_async
    async def test_forwards_type_value_for_structured_output(self, fake_llm):
        fake_llm.responses = ['{"a": 1}']
        assert await llmrag.Qx('Q', dict(a=int)) == {'a': 1}


# =============================================================================
# setup_llmrag()
# =============================================================================

class TestSetupLlmrag:
    def test_no_config_llm_is_a_no_op(self, fake_config):
        fake_config.llm = None
        llmrag._acompletion = None
        llmrag.setup_llmrag()
        assert llmrag._acompletion is None

    def test_host_two_element_form(self, fake_config, monkeypatch):
        monkeypatch.delenv('SOME_ENV', raising=False)
        fake_config.llm = ['host', 'http://localhost:11434/v1']
        llmrag.setup_llmrag()

        assert llmrag._acompletion is not None
        assert Unishare.llm_model == 'local-model'  # default when no model given
        client = llmrag._acompletion.__self__._client
        assert str(client.base_url) == 'http://localhost:11434/v1/'
        assert client.api_key == 'llm-studio'  # default when no api_key_env given

    def test_host_four_element_form_with_env_key(self, fake_config, monkeypatch):
        monkeypatch.setenv('MY_LOCAL_KEY', 'secret-123')
        fake_config.llm = ['host', 'http://localhost:8080/v1', 'MY_LOCAL_KEY', 'my-model']
        llmrag.setup_llmrag()

        assert Unishare.llm_model == 'my-model'
        client = llmrag._acompletion.__self__._client
        assert client.api_key == 'secret-123'

    def test_cloud_provider_two_element_form(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test-abc')
        fake_config.llm = ['openai', 'gpt-4o']
        llmrag.setup_llmrag()

        assert Unishare.llm_model == 'gpt-4o'
        client = llmrag._acompletion.__self__._client
        assert str(client.base_url) == 'https://api.openai.com/v1/'
        assert client.api_key == 'sk-test-abc'

    def test_cloud_provider_missing_env_key_falls_back_to_no_key(self, fake_config, monkeypatch):
        monkeypatch.delenv('OPENAI_API_KEY', raising=False)
        fake_config.llm = ['openai', 'gpt-4o']
        llmrag.setup_llmrag()
        client = llmrag._acompletion.__self__._client
        assert client.api_key == 'no-key'

    def test_cloud_provider_three_element_form_custom_address(self, fake_config, monkeypatch):
        monkeypatch.setenv('GROQ_API_KEY', 'gsk-test')
        fake_config.llm = ['groq', 'llama-3.1-70b-versatile', 'https://custom.groq.proxy/v1']
        llmrag.setup_llmrag()

        client = llmrag._acompletion.__self__._client
        assert str(client.base_url) == 'https://custom.groq.proxy/v1/'

    def test_google_and_gemini_aliases_share_base_url(self, fake_config, monkeypatch):
        monkeypatch.setenv('GOOGLE_API_KEY', 'g-key')
        expected = 'https://generativelanguage.googleapis.com/v1beta/openai/'

        fake_config.llm = ['google', 'gemini-3.1-pro-preview']
        llmrag.setup_llmrag()
        assert str(llmrag._acompletion.__self__._client.base_url) == expected

        fake_config.llm = ['gemini', 'gemini-3.1-pro-preview']
        llmrag.setup_llmrag()
        assert str(llmrag._acompletion.__self__._client.base_url) == expected

    def test_unknown_provider_does_not_set_a_client(self, fake_config):
        llmrag._acompletion = None
        fake_config.llm = ['not-a-real-provider', 'some-model']
        llmrag.setup_llmrag()
        assert llmrag._acompletion is None

    def test_invalid_shape_does_not_set_a_client(self, fake_config):
        llmrag._acompletion = None
        fake_config.llm = ['openai']  # too short to match any known shape
        llmrag.setup_llmrag()
        assert llmrag._acompletion is None

    def test_string_config_llm_is_treated_as_invalid_shape(self, fake_config):
        """
        A plain string does NOT match a list/sequence `case [...]` pattern
        in Python's structural pattern matching (str/bytes/bytearray are
        deliberately excluded from sequence-pattern matching, precisely to
        avoid a string being iterated character-by-character here) — so a
        mistaken `config.llm = 'openai'` falls straight through to the
        catch-all "invalid format" branch rather than doing something
        confusing with the individual characters.
        """
        llmrag._acompletion = None
        fake_config.llm = 'openai'
        llmrag.setup_llmrag()
        assert llmrag._acompletion is None

    def test_reasoning_config_sets_extra_body(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        fake_config.llm = ['openai', 'o3']
        fake_config.reasoning = 'high'
        llmrag.setup_llmrag()
        assert Unishare.llm_extra_body == {'reasoning': {'effort': 'high', 'enabled': True}}

    def test_no_reasoning_config_leaves_extra_body_none(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        fake_config.llm = ['openai', 'gpt-4o']
        llmrag.setup_llmrag()
        assert Unishare.llm_extra_body is None

    def test_temperature_config_is_read(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        fake_config.llm = ['openai', 'gpt-4o']
        fake_config.temperature = 0.7
        llmrag.setup_llmrag()
        assert Unishare.llm_temperature == 0.7

    def test_strict_schema_config_false(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        fake_config.llm = ['openai', 'gpt-4o']
        fake_config.strict_schema = False
        llmrag.setup_llmrag()
        assert Unishare.llm_strict_schema is False

    def test_strict_schema_defaults_to_true(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        fake_config.llm = ['openai', 'gpt-4o']
        llmrag.setup_llmrag()
        assert Unishare.llm_strict_schema is True

    def test_llm_cache_config_creates_a_query_cache(self, fake_config, monkeypatch, tmp_path):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        fake_config.llm = ['openai', 'gpt-4o']
        fake_config.llm_cache = str(tmp_path / 'cache_dir')
        llmrag.setup_llmrag()

        assert isinstance(Unishare.llm_cache, llmrag.QueryCache)
        Unishare.llm_cache.set('k', 'v')
        assert Unishare.llm_cache.get('k') == 'v'
        Unishare.llm_cache.close()

    def test_no_llm_cache_config_leaves_cache_unset(self, fake_config, monkeypatch):
        monkeypatch.setenv('OPENAI_API_KEY', 'sk-test')
        Unishare.llm_cache = None
        fake_config.llm = ['openai', 'gpt-4o']
        llmrag.setup_llmrag()
        assert Unishare.llm_cache is None


# =============================================================================
# get_property()
# =============================================================================

class TestGetProperty:
    @run_async
    async def test_extracts_the_requested_property(self, fake_llm):
        fake_llm.responses = ['1990-05-12']
        result = await llmrag.get_property('Founded year', context='The company was founded in 1990.')
        assert result == '1990-05-12'

    @run_async
    async def test_date_type_auto_detected_from_property_name(self, fake_llm):
        await llmrag.get_property('Date of birth', context='Born in 1990.')
        content = fake_llm.calls[0]['messages'][0]['content']
        # extend=False (format=False) here doesn't add the date instruction
        # itself, but effective_type='date' still drives _parse_response;
        # what we can observe from the sent prompt is the query line.
        assert 'Date of birth' in content

    @run_async
    async def test_options_are_embedded_in_the_prompt(self, fake_llm):
        await llmrag.get_property('Status', context='...', options=['active', 'closed'])
        content = fake_llm.calls[0]['messages'][0]['content']
        assert 'active,closed' in content

    @run_async
    async def test_returns_none_and_logs_when_the_llm_call_fails(self, fake_llm):
        logged = []
        Unishare.message_logger = lambda *args: logged.append(args)
        fake_llm.responses = ['not valid json']

        result = await llmrag.get_property('Info', context='...', type=dict(a=int))

        assert result is None
        assert logged  # the exception was routed to the logger, not raised
        # Regression guard for the get_property fix: it must call
        # message_logger with server.py's real (message: str, type: str)
        # contract — not a bare exception object, which is what the
        # pre-fix code passed even when a logger WAS configured.
        message, log_type = logged[0]
        assert isinstance(message, str) and 'Invalid JSON' in message
        assert log_type == 'error'

    @run_async
    async def test_returns_none_when_message_logger_is_not_configured(self, fake_llm, capsys):
        # Fixed bug: get_property() used to call Unishare.message_logger(exc)
        # directly with no callable guard, crashing with TypeError instead of
        # returning None whenever message_logger was unset (its default). It
        # now goes through _log(), which falls back to print() — see TestLog.
        Unishare.message_logger = None
        fake_llm.responses = ['not valid json']

        result = await llmrag.get_property('Info', context='...', type=dict(a=int))

        assert result is None
        assert '[error]' in capsys.readouterr().out
