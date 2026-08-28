"""
Unit tests for unisi/proxy.py -- the Proxy class, UNISI's client-side
helper for driving a screen over a raw websocket connection without a
browser (hot-connecting to a running session, scripted/automated
interaction, autotest-style tooling).

Proxy is architecturally simple compared to users.py/units.py: it holds no
reactive state, no persistence, no multi-user fan-out -- just a local mirror
of "the current screen" (a plain dict tree decoded from JSON) plus the
logic to read it (element/elements/commands/block_name) and keep it in sync
as messages arrive (process/update). So unlike tests/users or tests/units,
these tests never need a real server, a real User, or Unishare state --
only a scripted transport (see conftest.py's FakeConnection) standing in
for the real websocket.

Fixture screens/blocks/elements are never hand-typed as raw dicts: they're
built from the real unisi.units.Unit / unisi.containers.Block classes and
round-tripped through the real toJson()/json.loads() (conftest.py's
to_wire()/wire()), and 'update' messages for update()/process() tests are
built by driving the real unisi.common.Message / unisi.users.User.find_path
pipeline against real Block/Unit objects (conftest.py's real_update_message())
-- the same code users.py itself uses to build what actually goes out over
the wire. This means every fixture's shape is authentic by construction,
not a hand-guessed approximation of the wire format that could quietly
drift from what users.py really sends. The one exception is a handful of
update()-edge-case tests that deliberately build a malformed/unresolvable
update dict by hand (see TestUpdateEdgeCases) -- fill_paths4() would just
silently drop an entry find_path() can't resolve, so a real pipeline can't
produce that input at all; that's the point being tested.

Tests named test_regression_* each pin down a bug found while writing this
suite (and fixed alongside it, in proxy.py and, for the Dialog.commands
case, containers.py) -- see the module-level comments in proxy.py itself
("FIX ..." / "BUG (pre-fix): ...") for the full rationale of each one.
"""
import os
from urllib.parse import parse_qs

import pytest

import unisi.proxy as proxy_module
from unisi.proxy import Proxy, Event
from unisi.units import Edit, Button, Switch
from unisi.containers import Block, Dialog
from unisi.common import ArgObject

from conftest import make_proxy, screen_dict, to_wire, wire, real_update_message


# =============================================================================
# Event flags
# =============================================================================

class TestEventFlags:
    """Sanity-pin the bit relationships interact()'s progress loop, and the
    update_message/update_progress/update_complete/update_append/screen
    combos, depend on. Not proxy.py's own logic, but a change to these
    numeric values would silently break interact()'s `& Event.progress`
    loop-continuation check (and every `& Event.update` caller, per
    README.md's own command_upload example) without any test noticing.
    """

    def test_update_variants_share_the_update_bit(self):
        assert Event.update_message & Event.update
        assert Event.update_progress & Event.update
        assert Event.update_complete & Event.update
        assert Event.update_append & Event.update
        assert Event.screen & Event.update  # screen(65) = 64 | update(1)

    def test_plain_variants_do_not_have_the_update_bit(self):
        assert not (Event.message & Event.update)
        assert not (Event.progress & Event.update)
        assert not (Event.complete & Event.update)
        assert not (Event.append & Event.update)

    def test_progress_bit_present_on_both_progress_variants(self):
        assert Event.progress & Event.progress
        assert Event.update_progress & Event.progress

    def test_complete_and_append_do_not_look_like_progress(self):
        # interact()'s loop must stop on these, plain or update-carrying
        assert not (Event.complete & Event.progress)
        assert not (Event.append & Event.progress)
        assert not (Event.update_complete & Event.progress)
        assert not (Event.update_append & Event.progress)

    def test_none_is_falsy(self):
        assert not Event.none


# =============================================================================
# __init__ / connection setup
# =============================================================================

class TestInit:
    def test_ws_url_built_from_host_port(self, fake_conn):
        make_proxy(fake_conn, host_port='localhost:8000')
        assert fake_conn.address == 'ws://localhost:8000/ws'

    def test_wss_used_when_ssl_true(self, fake_conn):
        make_proxy(fake_conn, host_port='example.com:443', ssl=True)
        assert fake_conn.address == 'wss://example.com:443/ws'

    def test_https_host_port_used_for_upload_url_when_ssl_true(self, fake_conn):
        p = make_proxy(fake_conn, host_port='example.com:443', ssl=True)
        assert p.host_port == 'https://example.com:443'

    def test_http_host_port_used_for_upload_url_by_default(self, fake_conn):
        p = make_proxy(fake_conn, host_port='example.com:8000')
        assert p.host_port == 'http://example.com:8000'

    def test_trailing_slash_in_host_port_not_doubled(self, fake_conn):
        make_proxy(fake_conn, host_port='localhost:8000/')
        assert fake_conn.address == 'ws://localhost:8000/ws'

    def test_timeout_forwarded_to_create_connection(self, fake_conn):
        make_proxy(fake_conn, host_port='localhost:8000', timeout=15)
        assert fake_conn.timeout == 15

    def test_no_query_string_by_default(self, fake_conn):
        make_proxy(fake_conn, host_port='localhost:8000')
        assert '?' not in fake_conn.address

    def test_screen_param_appended_and_quoted(self, fake_conn):
        make_proxy(fake_conn, host_port='localhost:8000', screen='Panda params')
        assert fake_conn.address == 'ws://localhost:8000/ws?screen=Panda%20params'

    def test_regression_session_param_sent_as_a_named_query_key(self, fake_conn):
        # BUG (pre-fix): the raw session token was appended with no `session=`
        # key at all -- `?::1-0` instead of `?session=%3A%3A1-0` -- so
        # server.py's parse_qs(request.query_string) never saw a 'session'
        # key ('session' in parsed_query was always False) and silently
        # started a brand new session instead of reattaching to the given
        # one. This is the mechanism the "Hot connect to running session"
        # feature (test_apps/proxy/run_blocks.py) depends on.
        make_proxy(fake_conn, host_port='localhost:8000', session='::1-0')
        assert '?' in fake_conn.address
        parsed = parse_qs(fake_conn.address.split('?', 1)[1])
        assert parsed.get('session') == ['::1-0']

    def test_session_and_screen_both_appended_joined_by_ampersand(self, fake_conn):
        make_proxy(fake_conn, host_port='localhost:8000', session='abc-1', screen='Home')
        parsed = parse_qs(fake_conn.address.split('?', 1)[1])
        assert parsed.get('session') == ['abc-1']
        assert parsed.get('screen') == ['Home']

    def test_initial_screen_is_processed_during_construction(self, fake_conn):
        block = Block('Panel', Edit('X', '1'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.event == Event.screen
        assert p.screen['name'] == 'Home'
        assert 'name2block' in p.screen

    def test_non_screen_first_message_leaves_screen_none(self, fake_conn):
        p = make_proxy(fake_conn, {'type': 'error', 'value': 'connection refused'})
        assert p.screen is None
        assert p.event == Event.message


class TestClose:
    def test_close_closes_underlying_connection(self, fake_conn):
        p = make_proxy(fake_conn)
        assert fake_conn.closed is False
        p.close()
        assert fake_conn.closed is True


# =============================================================================
# Screen navigation: screen_menu / set_screen
# =============================================================================

class TestScreenMenu:
    def test_returns_names_only_from_menu_pairs(self, fake_conn):
        p = make_proxy(fake_conn, screen_dict('Home', menu=[['Home', 'home'], ['Settings', 'gear']]))
        assert p.screen_menu == ['Home', 'Settings']

    def test_empty_list_before_any_screen(self, fake_conn):
        p = make_proxy(fake_conn, {'type': 'error', 'value': 'nope'})
        assert p.screen is None
        assert p.screen_menu == []


class TestSetScreen:
    def test_returns_false_for_unknown_screen_name(self, fake_conn):
        p = make_proxy(fake_conn, screen_dict('Home', menu=[['Home', 'home']]))
        assert p.set_screen('Nowhere') is False
        assert fake_conn.sent == []  # never even asked the server

    def test_sends_root_message_and_returns_true_on_success(self, fake_conn):
        menu = [['Home', 'home'], ['Settings', 'gear']]
        p = make_proxy(fake_conn, screen_dict('Home', menu=menu))
        fake_conn.queue_message(screen_dict('Settings', menu=menu))
        assert p.set_screen('Settings') is True
        assert p.screen['name'] == 'Settings'
        assert fake_conn.last_sent == {'path': ['root'], 'value': 'Settings'}

    def test_returns_false_when_server_does_not_respond_with_a_screen(self, fake_conn):
        menu = [['Home', 'home'], ['Settings', 'gear']]
        p = make_proxy(fake_conn, screen_dict('Home', menu=menu))
        fake_conn.queue_message({'type': 'error', 'value': 'nope'})
        assert p.set_screen('Settings') is False
        assert len(fake_conn.sent) == 1  # the request was still made

    def test_always_requests_even_for_a_previously_seen_screen(self, fake_conn):
        # docstring: "Always sends a request... even when the screen was
        # visited before and is cached locally [in self.screens]."
        menu = [['Home', 'home'], ['Settings', 'gear']]
        p = make_proxy(fake_conn, screen_dict('Home', menu=menu))
        fake_conn.queue_message(screen_dict('Settings', menu=menu))
        p.set_screen('Settings')
        assert 'Home' in p.screens  # cached from construction

        fake_conn.queue_message(screen_dict('Home', menu=menu))
        p.set_screen('Home')
        assert len(fake_conn.sent) == 2  # asked the server again regardless


# =============================================================================
# Internal traversal helper: _iter_block_elements
# =============================================================================

class TestIterBlockElements:
    def test_yields_plain_elements_unchanged(self):
        value = to_wire(Edit('A', '1'), Switch('B', True))
        result = list(Proxy._iter_block_elements(value))
        assert [el['name'] for el in result] == ['A', 'B']

    def test_recurses_into_nested_blocks_without_yielding_the_block_itself(self):
        inner = Block('Inner', Edit('City', 'NYC'))
        value = to_wire(inner, Button('Go'))
        result = list(Proxy._iter_block_elements(value))
        assert [el['name'] for el in result] == ['City', 'Go']

    def test_flattens_nested_layout_rows(self):
        a, b, c = to_wire(Edit('A', '1'))[0], to_wire(Edit('B', '2'))[0], to_wire(Edit('C', '3'))[0]
        value = [a, [b, c]]
        result = list(Proxy._iter_block_elements(value))
        assert [el['name'] for el in result] == ['A', 'B', 'C']

    def test_skips_non_dict_items(self):
        assert list(Proxy._iter_block_elements([None, 'not-a-dict', 42])) == []


# =============================================================================
# Public element API: element() / elements() / commands
# =============================================================================

class TestElement:
    def test_finds_top_level_element(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.element('Name')['value'] == 'x'

    def test_finds_nested_element(self, fake_conn):
        inner = Block('Inner', Edit('City', 'NYC'))
        outer = Block('Outer', inner)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))
        assert p.element('City')['value'] == 'NYC'

    def test_returns_none_for_missing_element(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.element('Ghost') is None

    def test_returns_none_for_ambiguous_duplicate_names_across_blocks(self, fake_conn):
        block_a = Block('BlockA', Edit('Shared', 'a'))
        block_b = Block('BlockB', Edit('Shared', 'b'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block_a, block_b]))
        assert p.element('Shared') is None

    def test_restricts_search_to_named_block(self, fake_conn):
        block_a = Block('BlockA', Edit('Shared', 'a'))
        block_b = Block('BlockB', Edit('Shared', 'b'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block_a, block_b]))
        assert p.element('Shared', block_name='BlockB')['value'] == 'b'

    def test_block_name_filter_returns_none_for_unknown_block(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.element('Name', block_name='NoSuchBlock') is None

    def test_block_name_filter_also_searches_nested_children(self, fake_conn):
        inner = Block('Inner', Edit('City', 'NYC'))
        outer = Block('Outer', inner)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))
        assert p.element('City', block_name='Outer')['value'] == 'NYC'

    def test_regression_block_name_filter_does_not_crash_without_a_screen(self, fake_conn):
        # BUG (pre-fix): element(name, block_name=...) did
        # self.screen['name2block'] with no guard, raising TypeError instead
        # of gracefully returning None -- unlike the no-block_name path,
        # which _root_blocks() already guards (`if not self.screen: return`).
        p = make_proxy(fake_conn, {'type': 'error', 'value': 'nope'})
        assert p.screen is None
        assert p.element('Anything', block_name='SomeBlock') is None
        assert p.element('Anything') is None  # sibling path, already fine pre-fix


class TestElements:
    def test_returns_all_elements_when_unfiltered(self, fake_conn):
        block = Block('Panel', Edit('A', '1'), Switch('B', True))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert [el['name'] for el in p.elements()] == ['A', 'B']

    def test_filters_by_type(self, fake_conn):
        block = Block('Panel', Edit('A', '1'), Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert [el['name'] for el in p.elements(types=['command'])] == ['Go']

    def test_scoped_to_a_given_block_dict(self, fake_conn):
        block_a = Block('BlockA', Edit('A', '1'))
        block_b = Block('BlockB', Edit('B', '2'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block_a, block_b]))
        only_b = p.screen['name2block']['BlockB']
        assert [el['name'] for el in p.elements(block=only_b)] == ['B']

    def test_toolbar_included_in_unscoped_search(self, fake_conn):
        block = Block('Panel', Edit('A', '1'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block], toolbar=[Button('Logout')]))
        assert 'Logout' in [el['name'] for el in p.elements()]

    def test_empty_screen_returns_empty_list(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.elements() == []


class TestCommandsProperty:
    def test_returns_only_command_type_elements(self, fake_conn):
        block = Block('Panel', Edit('A', '1'), Button('Go'), Button('Cancel'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert [el['name'] for el in p.commands] == ['Go', 'Cancel']


# =============================================================================
# block_name() and the traversal helpers it's built on
# =============================================================================

class TestBlockName:
    def test_top_level_element_returns_its_block_name(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.block_name('Name') == 'Panel'

    def test_nested_element_returns_at_sign_joined_path(self, fake_conn):
        inner = Block('Inner', Edit('City', 'NYC'))
        outer = Block('Outer', inner)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))
        assert p.block_name('City') == 'Inner@Outer'

    def test_toolbar_element_returns_toolbar(self, fake_conn):
        p = make_proxy(fake_conn, screen_dict('Home', toolbar=[Button('Logout')]))
        assert p.block_name('Logout') == 'toolbar'

    def test_returns_none_for_unknown_element(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.block_name('Ghost') is None

    def test_accepts_a_dict_directly(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.block_name(p.element('Name')) == 'Panel'

    def test_regression_duplicate_named_elements_resolve_to_the_correct_block(self, fake_conn):
        # BUG (pre-fix): _owning_block matched purely by name (block_name()
        # threw away the actual dict and passed only its name down), so a
        # specific dict already in hand (e.g. one entry from elements())
        # could resolve to the WRONG block whenever some other, unrelated
        # element elsewhere happened to share its name -- whichever root
        # block came first always won, regardless of which object was
        # actually passed in.
        block_a = Block('BlockA', Edit('Shared', 'from-a'))
        block_b = Block('BlockB', Edit('Shared', 'from-b'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block_a, block_b]))
        b_dict = next(el for el in p.elements() if el['value'] == 'from-b')
        assert p.block_name(b_dict) == 'BlockB'


# =============================================================================
# Direct tests for the traversal helpers added by the update() fixes
# =============================================================================

class TestFindByNameInTree:
    """_find_by_name_in_tree() is what update()'s len(path) > 1 branch now
    uses instead of _iter_block_elements() (see TestUpdateBlock's nested-
    block regression test). update() always calls it starting from the
    target's *immediate* parent (path[1]), so in practice it only ever
    needs to match at the top level of that one call -- these tests cover
    its general/recursive behaviour directly, as its own unit.
    """

    def test_finds_a_top_level_element_by_name(self):
        value = to_wire(Edit('A', '1'), Edit('B', '2'))
        found = Proxy._find_by_name_in_tree(value, 'B')
        assert found is not None and found['value'] == '2'

    def test_finds_a_top_level_block_by_name_unlike_iter_block_elements(self):
        inner = Block('Inner', Edit('City', 'NYC'))
        value = to_wire(inner, Edit('Sibling', 'x'))
        found = Proxy._find_by_name_in_tree(value, 'Inner')
        assert found is not None and found['type'] == 'block'

    def test_recurses_past_a_non_matching_nested_block(self):
        deep = Block('Deep', Edit('Target', 'found-me'))
        wrapper = Block('Wrapper', deep)  # 'Wrapper' itself doesn't match
        value = to_wire(wrapper, Edit('Sibling', 'x'))
        found = Proxy._find_by_name_in_tree(value, 'Target')
        assert found is not None and found['value'] == 'found-me'

    def test_returns_none_when_nothing_matches(self):
        assert Proxy._find_by_name_in_tree(to_wire(Edit('A', '1')), 'Ghost') is None

    def test_skips_non_dict_items(self):
        assert Proxy._find_by_name_in_tree([None, 'not-a-dict', 42], 'Ghost') is None


class TestReplaceRootBlock:
    def test_replaces_a_block_nested_inside_a_layout_row(self, fake_conn):
        # self.screen['blocks'] commonly nests plain lists for layout rows
        # (e.g. [[block_a, block_b], block_c]); _replace_root_block must
        # see through those via flatten(), same as everywhere else here.
        a = Block('A', Edit('X', '1'))
        b = Block('B', Edit('Y', '1'))
        screen = screen_dict('Home', blocks=[[a, b]])
        p = make_proxy(fake_conn, screen)

        b.value = [Edit('Y', '2'), Edit('Z', 'new')]
        fake_conn.queue_message(real_update_message(b, blocks=[[a, b]]))
        p.request(None)

        assert p.element('Z') is not None  # tree-based lookup sees the row-nested replacement

    def test_is_a_no_op_for_an_unknown_block_name(self, fake_conn):
        block = Block('Panel', Edit('X', '1'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        p._replace_root_block('NoSuchBlock', {'name': 'NoSuchBlock', 'type': 'block', 'value': []})
        assert p.element('X')['value'] == '1'  # untouched, no crash


class TestElementAmbiguityWithinScopedBlock:
    def test_returns_none_for_ambiguous_names_within_a_scoped_block(self, fake_conn):
        # element(name, block_name=...) must apply the same ambiguity check
        # as the unscoped search when the scoped block itself contains
        # nested sub-blocks sharing an element name.
        sub_a = Block('SubA', Edit('Shared', 'a'))
        sub_b = Block('SubB', Edit('Shared', 'b'))
        outer = Block('Outer', sub_a, sub_b)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))
        assert p.element('Shared', block_name='Outer') is None


# =============================================================================
# File upload: upload() / command_upload()
# =============================================================================

class TestUpload:
    def test_posts_file_and_returns_response_text(self, fake_conn, tmp_path, monkeypatch):
        p = make_proxy(fake_conn, host_port='example.com:8000')
        fpath = tmp_path / 'photo.jpg'
        fpath.write_bytes(b'fake-image-bytes')

        captured = {}

        class FakeResponse:
            text = '/uploads/photo.jpg'

        def fake_post(url, files=None):
            captured['url'] = url
            captured['files'] = files
            return FakeResponse()

        monkeypatch.setattr(proxy_module.requests, 'post', fake_post)

        assert p.upload(str(fpath)) == '/uploads/photo.jpg'
        assert captured['url'] == p.host_port
        assert 'photo.jpg' in captured['files']

    def test_returns_empty_string_when_response_lacks_text(self, fake_conn, tmp_path, monkeypatch):
        p = make_proxy(fake_conn, host_port='example.com:8000')
        fpath = tmp_path / 'a.txt'
        fpath.write_text('hi')
        monkeypatch.setattr(proxy_module.requests, 'post', lambda url, files=None: object())
        assert p.upload(str(fpath)) == ''


class TestCommandUpload:
    def test_uses_local_abspath_when_host_is_localhost(self, fake_conn, tmp_path, monkeypatch):
        block = Block('Panel', Button('Load'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]), host_port='localhost:8000')
        fpath = tmp_path / 'img.jpg'
        fpath.write_bytes(b'x')

        monkeypatch.setattr(p, 'upload', lambda fp: (_ for _ in ()).throw(AssertionError('upload() should not be called for localhost')))
        fake_conn.queue_message({'type': 'update', 'updates': []})

        p.command_upload('Load', str(fpath))
        assert fake_conn.last_sent['value'] == os.path.abspath(str(fpath))

    def test_uploads_over_http_when_host_is_remote(self, fake_conn, tmp_path, monkeypatch):
        block = Block('Panel', Button('Load'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]), host_port='example.com:8000')
        fpath = tmp_path / 'img.jpg'
        fpath.write_bytes(b'x')

        monkeypatch.setattr(p, 'upload', lambda fp: '/server/img.jpg')
        fake_conn.queue_message({'type': 'update', 'updates': []})

        p.command_upload('Load', str(fpath))
        assert fake_conn.last_sent['value'] == '/server/img.jpg'

    def test_returns_invalid_when_upload_fails_without_sending_a_command(self, fake_conn, tmp_path, monkeypatch):
        block = Block('Panel', Button('Load'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]), host_port='example.com:8000')
        monkeypatch.setattr(p, 'upload', lambda fp: '')
        result = p.command_upload('Load', str(tmp_path / 'whatever.jpg'))
        assert result == Event.invalid
        assert fake_conn.sent == []


# =============================================================================
# make_message() / set_value() / command()
# =============================================================================

class TestMakeMessage:
    def test_builds_message_from_element_dict(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        msg = p.make_message(p.element('Name'), 'new-value')
        assert msg.path == ['Name', 'Panel']
        assert msg.event == 'changed'
        assert msg.value == 'new-value'

    def test_resolves_string_element_name(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.make_message('Name', 'new-value').path[0] == 'Name'

    def test_returns_none_for_missing_element(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.make_message('Ghost', 'x') is None

    def test_default_event_is_changed_regardless_of_element_keys(self, fake_conn):
        block = Block('Panel', Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        msg = p.make_message('Go')
        assert msg is not None and msg.event == 'changed'

    def test_returns_none_for_event_name_not_present_on_element(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))  # dict keys: name/value/x/type only
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.make_message('Name', event='dbl_clicked') is None

    def test_allows_event_name_present_on_element(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        # 'x' is a real key on a serialised Edit (its layout-offset field) --
        # a convenient stand-in for "any non-default key actually on the dict".
        assert 'x' in p.element('Name')
        msg = p.make_message('Name', value=5, event='x')
        assert msg is not None and msg.event == 'x'


class TestSetValue:
    def test_updates_local_value_and_sends_message(self, fake_conn):
        block = Block('Panel', Edit('Name', 'old'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'update', 'updates': []})

        event = p.set_value('Name', 'new')

        assert p.element('Name')['value'] == 'new'  # optimistic local update
        assert fake_conn.last_sent == {'path': ['Name', 'Panel'], 'event': 'changed', 'value': 'new'}
        assert event == Event.update

    def test_returns_invalid_for_missing_element_without_sending_anything(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.set_value('Ghost', 'x') == Event.invalid
        assert fake_conn.sent == []

    def test_accepts_an_element_dict_directly(self, fake_conn):
        active = Switch('Active', False)
        block = Block('Panel', active)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        el = p.element('Active')
        fake_conn.queue_message({'type': 'update', 'updates': []})
        p.set_value(el, True)
        assert el['value'] is True  # same dict object, mutated in place


class TestCommand:
    def test_sends_changed_event_with_no_value_by_default(self, fake_conn):
        block = Block('Panel', Button('Save'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'update', 'updates': []})

        event = p.command('Save')

        assert fake_conn.last_sent == {'path': ['Save', 'Panel'], 'event': 'changed', 'value': None}
        assert event == Event.update

    def test_passes_a_value_through(self, fake_conn):
        block = Block('Panel', Button('Upload'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'update', 'updates': []})
        p.command('Upload', '/tmp/x.jpg')
        assert fake_conn.last_sent['value'] == '/tmp/x.jpg'

    def test_regression_unknown_command_returns_invalid_without_touching_the_connection(self, fake_conn):
        # BUG (pre-fix): command() passed make_message()'s result straight
        # into interact() with no guard (unlike set_value(), which already
        # had `if ms else Event.invalid`). For an unknown command,
        # make_message() returns None, so interact(None) -> request(None)
        # skipped send() (message is falsy) but still called conn.recv()
        # unconditionally -- desyncing the request/response pairing (or
        # hanging, against a real socket) instead of failing fast.
        p = make_proxy(fake_conn)  # empty screen: no commands at all
        event = p.command('DoesNotExist')
        assert event == Event.invalid
        assert fake_conn.sent == []

    def test_regression_command_upload_with_unknown_command_does_not_hang(self, fake_conn, tmp_path):
        # Same bug as above, via command_upload()'s localhost/local-path
        # branch (so no requests.post mocking is needed to reach it).
        p = make_proxy(fake_conn, host_port='localhost:8000')  # no commands
        fpath = tmp_path / 'x.jpg'
        fpath.write_bytes(b'x')
        event = p.command_upload('DoesNotExist', str(fpath))
        assert event == Event.invalid
        assert fake_conn.sent == []


# =============================================================================
# Transport: request() / interact()
# =============================================================================

class TestRequestMethod:
    def test_sends_json_and_returns_the_processed_event(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'dialog', 'name': 'Sure?', 'commands': ['Ok'], 'value': []})
        event = p.request(ArgObject(path=['root'], value='X'))
        assert fake_conn.last_sent == {'path': ['root'], 'value': 'X'}
        assert event == Event.dialog

    def test_receives_without_sending_when_message_is_none(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'progress', 'value': 5})
        event = p.request(None)
        assert fake_conn.sent == []
        assert event == Event.progress


class TestInteract:
    def test_returns_immediately_for_a_non_progress_response(self, fake_conn):
        block = Block('Panel', Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'update', 'updates': []})
        event = p.interact(p.make_message('Go'))
        assert event == Event.update
        assert len(fake_conn.sent) == 1

    def test_loops_while_progress_and_invokes_the_callback_each_tick(self, fake_conn):
        block = Block('Panel', Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'progress', 'value': 10})
        fake_conn.queue_message({'type': 'progress', 'value': 50})
        fake_conn.queue_message({'type': 'update', 'updates': []})

        ticks = []
        event = p.interact(p.make_message('Go'), progress_callback=lambda proxy: ticks.append(proxy.message.get('value')))

        assert ticks == [10, 50]
        assert event == Event.update
        assert len(fake_conn.sent) == 1  # the callback ticks never re-send

    def test_keeps_looping_through_an_update_progress_tick_too(self, fake_conn):
        # update_progress (9 = progress(8) | update(1)) must still satisfy
        # `& Event.progress`, exactly like plain progress (8) -- and the
        # update it carries must actually get applied.
        status = Edit('Status', 'start')
        block = Block('Panel', status, Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        status.value = 'half'
        fake_conn.queue_message(real_update_message(status, blocks=[block], mtype='progress'))
        fake_conn.queue_message({'type': 'update', 'updates': []})

        event = p.interact(p.make_message('Go'))

        assert event == Event.update  # loop continued past the tick
        assert p.element('Status')['value'] == 'half'  # tick's update was applied


# =============================================================================
# Dialog: dialog_commands / dialog_responce
# =============================================================================

class TestDialog:
    def test_dialog_commands_empty_without_an_active_dialog(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.dialog_commands == []

    def test_dialog_commands_lists_the_button_names(self, fake_conn):
        # Regression for the companion containers.py bug: Dialog.__init__
        # never stored its own `commands` parameter anywhere on self, so the
        # serialised 'dialog' message had no 'commands' key at all and
        # `self.dialog['commands']` raised KeyError for any real dialog.
        p = make_proxy(fake_conn)
        dialog = Dialog('Delete this item?', lambda *_: None, commands=['Yes', 'No'])
        fake_conn.queue_message(wire(dialog))
        p.request(None)
        assert p.dialog_commands == ['Yes', 'No']

    def test_dialog_commands_defaults_to_ok_cancel(self, fake_conn):
        p = make_proxy(fake_conn)
        dialog = Dialog('Sure?', lambda *_: None)
        fake_conn.queue_message(wire(dialog))
        p.request(None)
        assert p.dialog_commands == ['Ok', 'Cancel']

    def test_dialog_responce_returns_invalid_without_an_active_dialog(self, fake_conn):
        p = make_proxy(fake_conn)
        assert p.dialog_responce('Ok') == Event.invalid
        assert p.event == Event.invalid
        assert fake_conn.sent == []

    def test_dialog_responce_sends_the_dialog_name_as_block_and_the_command_as_value(self, fake_conn):
        p = make_proxy(fake_conn)
        dialog = Dialog('Sure?', lambda *_: None)
        fake_conn.queue_message(wire(dialog))
        p.request(None)

        fake_conn.queue_message({'type': 'update', 'updates': []})
        event = p.dialog_responce('Ok')

        assert fake_conn.last_sent == {'path': ['Sure?'], 'value': 'Ok'}
        assert event == Event.update


# =============================================================================
# process(): message-type dispatch
# =============================================================================

class TestProcessScreen:
    def test_replaces_screen_and_builds_the_name2block_index(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.screen['name2block']['Panel']['name'] == 'Panel'
        assert p.screen['name2block']['toolbar'] == {'name': 'toolbar', 'value': []}

    def test_screen_cached_by_name_in_screens(self, fake_conn):
        p = make_proxy(fake_conn, screen_dict('Home'))
        assert p.screens['Home'] is p.screen

    def test_nested_blocks_indexed_too(self, fake_conn):
        inner = Block('Inner', Edit('City', 'NYC'))
        outer = Block('Outer', inner)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))
        assert 'Inner' in p.screen['name2block']
        assert 'Outer' in p.screen['name2block']


class TestProcessDialog:
    def test_stores_the_dialog_and_sets_the_dialog_event(self, fake_conn):
        p = make_proxy(fake_conn)
        dialog = Dialog('Sure?', lambda *_: None)
        fake_conn.queue_message(wire(dialog))
        event = p.request(None)
        assert event == Event.dialog
        assert p.dialog['name'] == 'Sure?'


class TestProcessComplete:
    def test_regression_complete_updates_self_event(self, fake_conn):
        # BUG (pre-fix): `elif mtype == 'complete': return Event.complete`
        # returned the right value up the call stack but skipped the
        # `self.event = ...` assignment every other branch does -- so
        # self.event (a documented, externally-read attribute; see
        # test_apps/proxy/run_blocks.py's own `if proxy.event == ...`
        # pattern) stayed stuck on whatever it was *before* this message.
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.event == Event.screen  # baseline, definitely not 'complete'

        fake_conn.queue_message({'type': 'complete', 'value': 'done', 'updates': []})
        event = p.request(None)

        assert event == Event.complete
        assert p.event == Event.complete

    def test_complete_without_updates_stays_the_plain_complete_event(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'complete', 'value': 'done'})
        event = p.request(None)
        assert event == Event.complete

    def test_regression_complete_applies_bundled_updates(self, fake_conn):
        # BUG (pre-fix): a 'complete' response can legitimately bundle
        # `updates` for OTHER units that changed as a side effect of the
        # same request (users.py's prepare_result folds self.changed_units
        # into any Message-typed raw result, complete/append included --
        # not just the explicit 'update' type). The 'complete' branch
        # never even looked at message['updates'], so such side-effect
        # changes were silently dropped, leaving the local screen mirror
        # stale with no error or signal.
        name_edit = Edit('Name', 'old')
        block = Block('Panel', name_edit)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        name_edit.value = 'new-from-complete'
        msg = real_update_message(name_edit, blocks=[block], mtype='complete')
        msg['value'] = 'task result'
        fake_conn.queue_message(msg)

        event = p.request(None)

        assert event == Event.update_complete
        assert p.element('Name')['value'] == 'new-from-complete'


class TestProcessAppend:
    def test_append_without_updates_sets_the_plain_append_event(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'append', 'value': {'row': 1}})
        event = p.request(None)
        assert event == Event.append

    def test_regression_append_applies_bundled_updates(self, fake_conn):
        # Same class of bug as TestProcessComplete's regression, for 'append'.
        status = Edit('Status', 'old')
        block = Block('Panel', status)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        status.value = 'new-from-append'
        msg = real_update_message(status, blocks=[block], mtype='append')
        msg['value'] = {'row': 'new'}
        fake_conn.queue_message(msg)

        event = p.request(None)

        assert event == Event.update_append
        assert p.element('Status')['value'] == 'new-from-append'


class TestProcessUpdate:
    def test_applies_updates_and_sets_the_update_event(self, fake_conn):
        name_edit = Edit('Name', 'old')
        block = Block('Panel', name_edit)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        name_edit.value = 'new'
        fake_conn.queue_message(real_update_message(name_edit, blocks=[block]))
        event = p.request(None)

        assert event == Event.update
        assert p.element('Name')['value'] == 'new'

    def test_regression_process_surfaces_unknown_update(self, fake_conn):
        # BUG (pre-fix): process()'s explicit 'update' branch unconditionally
        # set self.event = Event.update, discarding update()'s own verdict
        # (Event.unknown_update) whenever part of the batch couldn't be
        # matched against the local screen mirror -- silently hiding a
        # real "your local copy may now be stale" signal from the caller.
        block = Block('Panel', Edit('Name', 'old'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        # Deliberately malformed/unresolvable input -- hand-built rather
        # than via real_update_message(), whose fill_paths4() would just
        # drop an entry find_path() can't resolve rather than send it.
        fake_conn.queue_message({'type': 'update', 'updates': [
            {'path': ['Ghost', 'NoSuchBlock'], 'data': {'name': 'Ghost', 'value': 1}},
        ]})
        event = p.request(None)

        assert event == Event.unknown_update


class TestProcessGenericTypes:
    @pytest.mark.parametrize('mtype', ['error', 'warning', 'info'])
    def test_message_types_set_the_message_event(self, fake_conn, mtype):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': mtype, 'value': 'oops'})
        assert p.request(None) == Event.message

    @pytest.mark.parametrize('mtype', ['error', 'warning', 'info'])
    def test_message_types_with_updates_set_the_update_message_event(self, fake_conn, mtype):
        name_edit = Edit('Name', 'old')
        block = Block('Panel', name_edit)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        name_edit.value = 'fixed'
        msg = real_update_message(name_edit, blocks=[block], mtype=mtype)
        msg['value'] = 'oops'
        fake_conn.queue_message(msg)

        event = p.request(None)

        assert event == Event.update_message
        assert p.element('Name')['value'] == 'fixed'

    def test_progress_sets_the_progress_event(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'progress', 'value': 30})
        assert p.request(None) == Event.progress

    def test_progress_with_updates_sets_the_update_progress_event(self, fake_conn):
        name_edit = Edit('Name', 'old')
        block = Block('Panel', name_edit)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        name_edit.value = 'ticking'
        fake_conn.queue_message(real_update_message(name_edit, blocks=[block], mtype='progress'))
        assert p.request(None) == Event.update_progress

    def test_a_completely_unrecognised_type_sets_the_unknown_event(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'something-new-and-unhandled'})
        assert p.request(None) == Event.unknown

    def test_a_completely_unrecognised_type_with_updates_sets_unknown_update(self, fake_conn):
        name_edit = Edit('Name', 'old')
        block = Block('Panel', name_edit)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        name_edit.value = 'via-unknown-type'
        fake_conn.queue_message(real_update_message(name_edit, blocks=[block], mtype='something-new-and-unhandled'))

        event = p.request(None)

        assert event == Event.unknown_update
        assert p.element('Name')['value'] == 'via-unknown-type'


class TestProcessEmptyMessage:
    def test_a_null_payload_sets_event_none(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_raw('null')  # server sent literal JSON null
        event = p.request(None)
        assert event == Event.none
        assert p.mtype is None


# =============================================================================
# update(): applying incremental changes to the local screen mirror
# =============================================================================

class TestUpdateElement:
    def test_updates_a_top_level_element_value(self, fake_conn):
        name_edit = Edit('Name', 'old')
        block = Block('Panel', name_edit)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        name_edit.value = 'new'
        fake_conn.queue_message(real_update_message(name_edit, blocks=[block]))
        p.request(None)
        assert p.element('Name')['value'] == 'new'

    def test_updates_a_nested_element_value_leaving_siblings_alone(self, fake_conn):
        city = Edit('City', 'NYC')
        inner = Block('Inner', city)
        outer = Block('Outer', inner, Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))

        city.value = 'Boston'
        fake_conn.queue_message(real_update_message(city, blocks=[outer]))
        p.request(None)

        assert p.element('City')['value'] == 'Boston'
        assert p.element('Go') is not None

    def test_updates_a_toolbar_element(self, fake_conn):
        logout = Button('Logout')
        p = make_proxy(fake_conn, screen_dict('Home', toolbar=[logout]))

        logout.value = 'server-set-value'
        fake_conn.queue_message(real_update_message(logout, blocks=[], toolbar=[logout]))
        p.request(None)

        assert p.element('Logout', block_name='toolbar')['value'] == 'server-set-value'


class TestUpdateBlock:
    def test_replaces_a_root_blocks_value_in_the_flat_index(self, fake_conn):
        block = Block('Panel', Edit('X', '1'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        block.value = [Edit('X', '2'), Edit('Y', 'new')]
        fake_conn.queue_message(real_update_message(block, blocks=[block]))
        p.request(None)

        names = [el['name'] for el in p.screen['name2block']['Panel']['value']]
        assert names == ['X', 'Y']

    def test_regression_root_block_replacement_syncs_the_root_blocks_tree(self, fake_conn):
        # BUG (pre-fix): update()'s length-1-path branch did
        # `name2block[block_name] = data`, which only rebinds the flat
        # index entry -- it never touches self.screen['blocks'], the
        # actual tree _root_blocks() (and therefore elements()/commands/
        # element() without a block_name/_owning_block) reads. So after a
        # whole-root-block replacement, direct name2block-based lookups
        # (element(name, block_name=...)) saw fresh data while every
        # tree-walking lookup kept serving the stale block indefinitely.
        block = Block('Panel', Edit('X', '1'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        assert p.element('Y') is None  # doesn't exist yet

        block.value = [Edit('X', '2'), Edit('Y', 'new')]
        fake_conn.queue_message(real_update_message(block, blocks=[block]))
        p.request(None)

        assert p.screen['name2block']['Panel']['value'][1]['name'] == 'Y'  # flat index: fresh
        assert p.element('Y') is not None  # tree-based lookup: must ALSO be fresh
        assert p.element('Y')['value'] == 'new'
        assert 'Y' in [el['name'] for el in p.elements()]

    def test_regression_nested_block_replacement_applies(self, fake_conn):
        # BUG (pre-fix): a nested (non-root) block that itself changed as a
        # whole gets a path like ['Inner', 'Outer'] (own name, then direct
        # parent -- see users.py's find_path). update()'s len(path) > 1
        # branch searched for that target via _iter_block_elements(), which
        # -- correctly, for its OTHER callers element()/elements() -- never
        # yields block-typed items themselves, only recurses into them.
        # That's right for "give me the leaf units", wrong for update()'s
        # "find the thing named X", which must also match X when X is
        # itself a nested block: such updates were silently dropped as
        # Event.unknown_update and the block's contents never refreshed.
        inner = Block('Inner', Edit('City', 'NYC'))
        outer = Block('Outer', inner, Button('Go'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))

        inner.value = [Edit('City', 'Boston'), Edit('Zip', '02108')]
        fake_conn.queue_message(real_update_message(inner, blocks=[outer]))
        event = p.request(None)

        assert event == Event.update  # not unknown_update
        assert p.element('City')['value'] == 'Boston'
        assert p.element('Zip')['value'] == '02108'
        assert p.element('Go') is not None  # outer block's other child untouched

    def test_regression_nested_block_replacement_reindexes_its_own_children(self, fake_conn):
        # A nested-block replacement that introduces a FURTHER-nested block
        # (brand new to name2block) must also get that new block indexed --
        # mirrors the existing re-indexing already done for root-block
        # replacement.
        inner = Block('Inner', Edit('City', 'NYC'))
        outer = Block('Outer', inner)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[outer]))

        deepest = Block('Deepest', Edit('Zip', '02108'))
        inner.value = [deepest]
        fake_conn.queue_message(real_update_message(inner, blocks=[outer]))
        p.request(None)

        assert 'Deepest' in p.screen['name2block']
        assert p.element('Zip', block_name='Deepest')['value'] == '02108'


class TestUpdateEdgeCases:
    def test_empty_path_marks_unknown_update(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'update', 'updates': [{'path': [], 'data': {}}]})
        assert p.request(None) == Event.unknown_update

    def test_unknown_top_level_block_name_marks_unknown_update(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'update', 'updates': [
            {'path': ['Ghost'], 'data': {'name': 'Ghost'}},
        ]})
        assert p.request(None) == Event.unknown_update

    def test_unknown_parent_block_name_marks_unknown_update(self, fake_conn):
        p = make_proxy(fake_conn)
        fake_conn.queue_message({'type': 'update', 'updates': [
            {'path': ['X', 'Ghost'], 'data': {'name': 'X', 'value': 1}},
        ]})
        assert p.request(None) == Event.unknown_update

    def test_unknown_element_name_within_a_known_block_marks_unknown_update(self, fake_conn):
        block = Block('Panel', Edit('Name', 'x'))
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))
        fake_conn.queue_message({'type': 'update', 'updates': [
            {'path': ['Ghost', 'Panel'], 'data': {'name': 'Ghost', 'value': 1}},
        ]})
        assert p.request(None) == Event.unknown_update

    def test_multiple_updates_in_one_message_are_all_applied(self, fake_conn):
        a, b = Edit('A', '1'), Edit('B', '2')
        block = Block('Panel', a, b)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        a.value, b.value = 'A2', 'B2'
        msg_a = real_update_message(a, blocks=[block])
        msg_b = real_update_message(b, blocks=[block])
        fake_conn.queue_message({'type': 'update', 'updates': msg_a['updates'] + msg_b['updates']})

        event = p.request(None)

        assert event == Event.update
        assert p.element('A')['value'] == 'A2'
        assert p.element('B')['value'] == 'B2'

    def test_one_bad_entry_does_not_block_the_rest_of_the_batch(self, fake_conn):
        a = Edit('A', '1')
        block = Block('Panel', a)
        p = make_proxy(fake_conn, screen_dict('Home', blocks=[block]))

        a.value = 'A2'
        good = real_update_message(a, blocks=[block])['updates']
        bad = [{'path': ['Ghost', 'Panel'], 'data': {'name': 'Ghost'}}]
        fake_conn.queue_message({'type': 'update', 'updates': good + bad})

        event = p.request(None)

        assert event == Event.unknown_update  # overall verdict reflects the bad entry...
        assert p.element('A')['value'] == 'A2'  # ...but the good one still applied
