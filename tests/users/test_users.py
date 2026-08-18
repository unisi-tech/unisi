"""
Unit tests for unisi/users.py -- the User class's own message-dispatch,
multi-user fan-out, and screen-lifecycle logic.

Deliberately does NOT re-cover what tests/persist_voice_reloder/ already
does thoroughly: find_path, register_changed_unit's echo-suppression rules,
prepare_result's raw-shape handling, find_element (test_messaging.py),
get_objects/get_contexts/persist_location (test_object_search.py), or
VoiceCom's own internals (test_voicecom.py). This file covers construction,
process()/process_element()/eval_handler() dispatch details, screen
navigation, broadcast/reflect fan-out, progress, delete, activate_session,
update_menu, calc_dbsharing/sync_dbupdates, compile_screen's testing-mode
behaviour, run_process, sync_send, and init_user.

Uses real User instances against fixtures_app/ (see conftest.py) rather
than mocking users.py's own dependencies -- same rationale as
tests/persist_voice_reloder/conftest.py.
"""
import asyncio

import pytest

from unisi.common import Message, ReceivedMessage, Unishare
from unisi.containers import Dialog
from unisi.units import Edit
from unisi.utils import testdir


def received(block, element, event, value=None):
    """Shorthand for building an incoming client message."""
    return ReceivedMessage({"block": block, "element": element, "event": event, "value": value})


class FakeReflectionUser:
    """
    A minimal stand-in for another connected User sharing this one's
    `reflections` list, for testing broadcast()/reflect()'s fan-out logic
    without a second real (websocket-backed) User -- only .screen_module
    (compared for identity) and an async .send() (recorded here) are ever
    touched by that logic.
    """
    def __init__(self, screen_module=None):
        self.screen_module = screen_module
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


# =============================================================================
# Construction
# =============================================================================

class TestConstruction:
    def test_testing_session_still_auto_loads_a_screen(self, make_user):
        # load_lazy() doesn't check self.testing -- testing mode only
        # affects OTHER behaviour (persistence, progress/monitor no-ops),
        # not whether a screen gets auto-loaded on construction.
        user = make_user(session=testdir)
        assert user.testing is True
        assert user.screen_module is not None

    def test_non_testing_session_auto_loads_the_default_screen(self, make_user):
        user = make_user()
        assert user.testing is False
        assert user.screen_module is not None
        assert user.screen_module.name in ("Home", "Other")  # whichever sorts first

    def test_sets_class_level_last_user(self, make_user):
        from unisi.users import User
        user = make_user()
        assert User.last_user is user

    def test_fresh_instance_state(self, make_user):
        user = make_user(session=testdir)
        assert user.active_dialog is None
        assert user.last_message is None
        assert user.changed_units == set()
        assert user.voice is None
        assert user.reflections == []
        assert user.handlers == {}

    def test_no_share_has_no_send_method(self, make_user):
        # send is only ever attached post-hoc (server.py's websocket
        # handler in production; wire_send() in these tests).
        user = make_user(session=testdir)
        assert not hasattr(user, "send")

    def test_share_copies_screens_and_handlers(self, make_user):
        parent = make_user("home")
        parent.handlers = {("el", "changed"): "handler"}

        from unisi.users import User
        child = User("child-of-" + parent.session, share=parent)

        assert child.screens is parent.screens
        assert child.handlers is parent.handlers
        assert child.screen_module is parent.screen_module  # config.mirror is False by default

    def test_share_mirror_mode_uses_first_screen(self, make_user, monkeypatch):
        import config
        parent = make_user("other")  # not the alphabetically-first screen
        assert parent.screens[0].name != parent.screen_module.name or len(parent.screens) == 1

        monkeypatch.setattr(config, "mirror", True, raising=False)
        from unisi.users import User
        child = User("child-of-" + parent.session, share=parent)

        assert child.screen_module is parent.screens[0]

    def test_share_establishes_mutual_reflections(self, make_user):
        parent = make_user("home")
        assert parent.reflections == []

        from unisi.users import User
        child = User("child-of-" + parent.session, share=parent)

        assert parent.reflections == [parent, child]
        assert child.reflections is parent.reflections

    def test_share_with_screen_name_selects_that_screen_if_found(self, make_user):
        parent = make_user("home")
        from unisi.users import User
        child = User("child-of-" + parent.session, share=parent, screen="Other")
        assert child.screen_module.name == "Other"

    def test_share_with_unknown_screen_name_keeps_shared_default(self, make_user):
        parent = make_user("home")
        from unisi.users import User
        child = User("child-of-" + parent.session, share=parent, screen="NoSuchScreen")
        assert child.screen_module is parent.screen_module


# =============================================================================
# Simple properties
# =============================================================================

class TestProperties:
    def test_testing_true_for_testdir_session(self, make_user):
        assert make_user(session=testdir).testing is True

    def test_testing_false_for_a_real_session(self, make_user):
        assert make_user().testing is False

    def test_id_is_session(self, make_user):
        user = make_user(session=testdir)
        assert user.id == user.session == testdir

    def test_global_persist_reflects_config(self, make_user, monkeypatch):
        import config
        user = make_user(session=testdir)
        monkeypatch.setattr(config, "persist", False, raising=False)
        assert user._global_persist is False
        monkeypatch.setattr(config, "persist", True, raising=False)
        assert user._global_persist is True


class TestSortedChangedUnits:
    def test_empty_set(self, make_user):
        assert make_user(session=testdir).sorted_changed_units == set()

    def test_single_unit_returned_as_is(self, make_user):
        user = make_user("home")
        edit = Edit("Solo", "1")
        user.changed_units = {edit}
        assert user.sorted_changed_units is user.changed_units

    def test_blocks_sort_first(self, make_user):
        from unisi.containers import Block
        user = make_user("home")
        edit = Edit("A", "1")
        block = Block("B")
        user.changed_units = {edit, block}
        result = list(user.sorted_changed_units)
        assert result[0] is block
        assert result[1] is edit


class TestBlocksProperty:
    def test_no_active_dialog_returns_screen_blocks(self, make_user):
        user = make_user("home")
        assert list(user.blocks) == list(user.screen.blocks)

    def test_active_dialog_with_a_value_is_prepended(self, make_user):
        user = make_user("home")
        dialog = Dialog("Sure?", lambda *_: None)
        user.active_dialog = dialog
        result = user.blocks
        assert result[0] is dialog
        assert list(result[1:]) == list(user.screen.blocks)

    def test_active_dialog_without_a_value_is_not_prepended(self, make_user):
        user = make_user("home")
        dialog = Dialog("Sure?", lambda *_: None)
        dialog.value = None
        user.active_dialog = dialog
        assert list(user.blocks) == list(user.screen.blocks)


# =============================================================================
# _iter_units / assign_parent_links / _refresh_parents_for_block
# =============================================================================

class TestIterUnitsAndParents:
    def test_iter_units_yields_blocks_and_leaves(self, make_user):
        user = make_user("home")
        mod = user.screen_module
        units = set(user._iter_units())
        assert mod.root in units
        assert mod.save_button in units
        assert mod.plain_edit in units

    def test_iter_units_includes_toolbar(self, make_user):
        user = make_user("home")
        assert user.screen_module.help_button in set(user._iter_units())

    def test_assign_parent_links_maps_children_to_containers(self, make_user):
        user = make_user("home")
        mod = user.screen_module
        parents = mod.screen._parents
        assert parents[mod.root] is mod.screen
        assert parents[mod.save_button] is mod.root

    def test_refresh_parents_for_block_rebuilds_when_missing(self, make_user):
        user = make_user("home")
        mod = user.screen_module
        object.__setattr__(mod.screen, "_parents", None)

        user._refresh_parents_for_block(mod.root)

        assert mod.screen._parents is not None
        assert mod.screen._parents[mod.save_button] is mod.root

    def test_refresh_parents_for_block_uses_explicit_new_value_over_stale_attribute(self, make_user):
        """
        Regression test for a fixed bug: Unit.__setattr__ calls
        _mark_changed(name, value) -- which is what leads to
        register_changed_unit() -> _refresh_parents_for_block() -- BEFORE
        its own super().__setattr__(name, value) actually stores the new
        value, so block.value is still the OLD value at the point this
        normally runs. register_changed_unit() now passes its own `value`
        argument through explicitly instead of this method re-reading
        block.value itself, so a real `some_block.value = [...]`
        reassignment correctly registers the NEW children.
        """
        from unisi.containers import Block
        user = make_user("home")
        mod = user.screen_module
        user.last_message = received("other", "X", "changed", None)

        new_child = Edit("NewChild", "2")
        mod.root.value = [new_child]  # a completely ordinary reassignment

        assert list(mod.root.value) == [new_child]
        assert mod.screen._parents[new_child] is mod.root

    def test_refresh_parents_for_block_direct_call_falls_back_to_current_value(self, make_user):
        # Direct callers (no new_value given) still work off block.value
        # itself, e.g. after the real assignment has already landed.
        user = make_user("home")
        mod = user.screen_module
        new_child = Edit("DirectChild", "3")
        object.__setattr__(mod.root, "value", [new_child])

        user._refresh_parents_for_block(mod.root)

        assert mod.screen._parents[new_child] is mod.root


# =============================================================================
# eval_handler
# =============================================================================

class TestEvalHandler:
    @pytest.mark.asyncio
    async def test_calls_async_handler(self, make_user):
        user = make_user(session=testdir)
        async def handler(elem, value):
            return f"async {elem} {value}"
        assert await user.eval_handler(handler, "el", "val") == "async el val"

    @pytest.mark.asyncio
    async def test_calls_sync_handler(self, make_user):
        user = make_user(session=testdir)
        def handler(elem, value):
            return f"sync {elem} {value}"
        assert await user.eval_handler(handler, "el", "val") == "sync el val"

    @pytest.mark.asyncio
    async def test_propagates_handler_exceptions(self, make_user):
        user = make_user(session=testdir)
        def handler(elem, value):
            raise ValueError("boom")
        with pytest.raises(ValueError, match="boom"):
            await user.eval_handler(handler, "el", "val")

    @pytest.mark.asyncio
    async def test_notifies_the_monitor_around_the_call_when_active(self, make_user, monkeypatch):
        import unisi.users as users_module
        user = make_user(session=testdir)
        notified = []
        async def fake_notify(kind, session, message):
            notified.append(kind)
        monkeypatch.setattr(users_module, "notify_monitor", fake_notify)

        async def handler(elem, value):
            assert notified == ["+"]
            return "ok"
        await user.eval_handler(handler, "el", "val")

        assert notified == ["+", "-"]


# =============================================================================
# process_element
# =============================================================================

class TestProcessElement:
    @pytest.mark.asyncio
    async def test_calls_the_element_own_changed_handler(self, make_user):
        user = make_user("home")
        mod = user.screen_module

        result = await user.process_element(mod.save_button, received("Root", "Save", "changed", "v"))

        assert result == "handled"
        assert mod.save_clicks[-1] == "v"

    @pytest.mark.asyncio
    async def test_edit_without_a_handler_just_assigns_the_value(self, make_user):
        user = make_user("home")
        mod = user.screen_module

        result = await user.process_element(mod.plain_edit, received("Root", "Plain", "changed", "new"))

        assert result is None
        assert mod.plain_edit.value == "new"

    @pytest.mark.asyncio
    async def test_handler_registered_in_self_handlers_dict_takes_priority(self, make_user):
        user = make_user("home")
        mod = user.screen_module
        calls = []
        user.handlers[(mod.plain_edit, "changed")] = lambda elem, value: calls.append(value) or "via-dict"

        result = await user.process_element(mod.plain_edit, received("Root", "Plain", "changed", "Y"))

        assert result == "via-dict"
        assert calls == ["Y"]

    @pytest.mark.asyncio
    async def test_complete_event_wraps_the_result_in_an_answer(self, make_user):
        user = make_user("home")
        mod = user.screen_module

        result = await user.process_element(mod.completable, received("Root", "Completable", "complete", "q"))

        assert isinstance(result, Message)
        assert result.type == "complete"
        assert result.value == "completion-result"

    @pytest.mark.asyncio
    async def test_non_changed_event_with_a_matching_attribute_sets_it(self, make_user):
        user = make_user("home")
        mod = user.screen_module

        result = await user.process_element(mod.attributed, received("Root", "Attributed", "custom_attr", "new-val"))

        assert result is None
        assert mod.attributed.custom_attr == "new-val"

    @pytest.mark.asyncio
    async def test_non_changed_event_with_no_matching_handler_or_attribute_returns_error(self, make_user):
        user = make_user("home")
        mod = user.screen_module

        result = await user.process_element(
            mod.plain_edit, received("Root", "Plain", "totally_unknown_event", "x")
        )

        assert isinstance(result, Message)
        assert result.type == "error"
        assert "doesn't contain" in result.value


# =============================================================================
# process
# =============================================================================

class TestProcess:
    @pytest.mark.asyncio
    async def test_routes_to_the_matching_element(self, make_user):
        user = make_user("home")
        mod = user.screen_module

        await user.process(received("Root", "Save", "changed", "x"))

        assert mod.save_clicks[-1] == "x"

    @pytest.mark.asyncio
    async def test_unknown_element_returns_an_error_message(self, make_user):
        user = make_user("home")
        result = await user.process(received("Root", "NoSuchElement", "changed", None))
        assert isinstance(result, Message)
        assert "does not exist" in result.value

    @pytest.mark.asyncio
    async def test_voice_message_creates_a_voicecom_instance(self, make_user):
        from unisi.voicecom import VoiceCom
        user = make_user("home")
        assert user.voice is None

        await user.process(ReceivedMessage({"block": "voice", "element": None, "event": "changed", "value": "hi"}))

        assert isinstance(user.voice, VoiceCom)

    @pytest.mark.asyncio
    async def test_voice_listen_true_starts_listening(self, make_user):
        user = make_user("home")
        listen_msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": True})

        await user.process(listen_msg)

        assert user.voice is not None

    @pytest.mark.asyncio
    async def test_voice_listen_false_stops_listening(self, make_user):
        user = make_user("home")
        stopped = []
        user.voice = type("FakeVoice", (), {"stop": lambda self: stopped.append(True)})()
        listen_msg = ReceivedMessage({"block": "voice", "element": None, "event": "listen", "value": False})

        await user.process(listen_msg)

        assert stopped == [True]


# =============================================================================
# result4message: dialog handling (message-dispatch details, not the raw-shape
# handling already covered in tests/persist_voice_reloder/test_messaging.py)
# =============================================================================

class TestResult4MessageDialogs:
    @pytest.mark.asyncio
    async def test_sets_last_message_before_processing(self, make_user):
        user = make_user("home")
        msg = received("Root", "Plain", "changed", "x")
        await user.result4message(msg)
        assert user.last_message is msg

    @pytest.mark.asyncio
    async def test_dialog_button_click_routes_through_process_element(self, make_user, wire_send):
        user = make_user("home")
        wire_send(user)
        seen = []
        dialog = Dialog("Sure?", lambda user, value: seen.append(value))
        user.active_dialog = dialog
        ok_button = dialog.value[0]

        await user.result4message(received(dialog.name, ok_button.name, "changed", None))

        assert seen == ["Ok"]
        assert user.active_dialog is None

    @pytest.mark.asyncio
    async def test_dialog_message_with_no_element_calls_dialog_changed_directly(self, make_user, wire_send):
        user = make_user("home")
        wire_send(user)
        partner = FakeReflectionUser(user.screen_module)
        user.reflections = [user, partner]
        seen = []
        dialog = Dialog("Sure?", lambda user, value: seen.append(value))
        user.active_dialog = dialog

        await user.result4message(
            ReceivedMessage({"block": dialog.name, "element": None, "event": "changed", "value": "Ok"})
        )

        assert seen == ["Ok"]
        assert user.active_dialog is None
        assert partner.sent  # close_message was broadcast to the other reflected session

    @pytest.mark.asyncio
    async def test_dialog_handler_returning_a_new_dialog_opens_it(self, make_user, wire_send):
        user = make_user("home")
        wire_send(user)
        second_dialog = Dialog("Really sure?", lambda user, value: None)
        first_dialog = Dialog("Sure?", lambda user, value: second_dialog)
        user.active_dialog = first_dialog
        ok_button = first_dialog.value[0]

        await user.result4message(received(first_dialog.name, ok_button.name, "changed", None))

        assert user.active_dialog is second_dialog


# =============================================================================
# screen_process / set_screen
# =============================================================================

class TestScreenProcessAndSetScreen:
    @pytest.mark.asyncio
    async def test_navigating_to_the_current_screen_returns_true(self, make_user):
        user = make_user("home")
        msg = ReceivedMessage({"block": "root", "element": None, "value": "Home"})
        assert user.screen_process(msg) is True

    def test_navigating_to_an_unknown_screen_returns_an_error(self, make_user):
        user = make_user("home")
        msg = ReceivedMessage({"block": "root", "element": None, "value": "NoSuchScreen"})
        result = user.screen_process(msg)
        assert isinstance(result, Message)
        assert "Unknown screen name" in result.value

    def test_real_navigation_to_a_different_screen_starts_voice_and_prepares(self, make_user):
        user = make_user("home")
        other = user.ensure_screen("Other")
        other.prepared.clear()

        class FakeVoice:
            def __init__(self):
                self.calls = []
            def set_screen(self, s):
                self.calls.append(("set_screen", s))
            def start(self):
                self.calls.append(("start",))
        user.voice = FakeVoice()

        result = user.screen_process(ReceivedMessage({"block": "root", "element": None, "value": "Other"}))

        assert result is True
        assert user.screen_module.name == "Other"
        assert user.voice.calls == [("set_screen", other.screen), ("start",)]
        assert other.prepared == [1]

    def test_navigating_to_the_same_screen_object_skips_prepare(self, make_user):
        user = make_user("home")
        home = user.screen_module

        result = user.screen_process(ReceivedMessage({"block": "root", "element": None, "value": "Home"}))

        assert result is True
        assert user.screen_module is home

    def test_set_screen_switches_to_a_known_screen(self, make_user):
        user = make_user("home")
        user.set_screen("Other")
        assert user.screen_module.name == "Other"

    def test_set_screen_unknown_name_returns_an_error(self, make_user):
        user = make_user("home")
        result = user.set_screen("NoSuchScreen")
        assert isinstance(result, Message)
        assert "Unknown screen name" in result.value


# =============================================================================
# broadcast / reflect
# =============================================================================

class TestBroadcast:
    @pytest.mark.asyncio
    async def test_sends_only_to_other_users_on_the_same_screen(self, make_user):
        user = make_user("home")
        same_screen = FakeReflectionUser(user.screen_module)
        other_screen = FakeReflectionUser(object())
        user.reflections = [user, same_screen, other_screen]

        await user.broadcast("hello")

        assert same_screen.sent == ["hello"]
        assert other_screen.sent == []

    @pytest.mark.asyncio
    async def test_never_sends_to_self(self, make_user, wire_send):
        user = make_user("home")
        wire_send(user)
        user.reflections = [user]
        await user.broadcast("hello")  # must not raise / must not call self.send

    @pytest.mark.asyncio
    async def test_non_string_messages_are_prepared_and_serialised_to_json(self, make_user):
        import json
        user = make_user("home")
        partner = FakeReflectionUser(user.screen_module)
        user.reflections = [user, partner]

        await user.broadcast(user.screen_module.plain_edit)

        assert len(partner.sent) == 1
        payload = json.loads(partner.sent[0])
        assert payload["updates"][0]["data"]["name"] == "Plain"


class TestReflect:
    @pytest.mark.asyncio
    async def test_no_reflections_is_a_noop(self, make_user):
        user = make_user("home")
        user.reflections = []
        await user.reflect(None, "hello")  # must not raise

    @pytest.mark.asyncio
    async def test_broadcasts_a_truthy_result_with_no_message(self, make_user):
        user = make_user("home")
        partner = FakeReflectionUser(user.screen_module)
        user.reflections = [user, partner]

        await user.reflect(None, "progress update")

        assert partner.sent == ["progress update"]

    @pytest.mark.asyncio
    async def test_screen_navigation_messages_are_never_reflected(self, make_user):
        user = make_user("home")
        partner = FakeReflectionUser(user.screen_module)
        user.reflections = [user, partner]
        nav_msg = ReceivedMessage({"block": "root", "element": None, "value": "Home"})

        await user.reflect(nav_msg, "some result")

        assert partner.sent == []

    @pytest.mark.asyncio
    async def test_result_not_covering_the_targeted_element_also_broadcasts_it(self, make_user):
        user = make_user("home")
        mod = user.screen_module
        partner = FakeReflectionUser(mod)
        user.reflections = [user, partner]
        msg = received("Root", "Plain", "changed", "x")
        result = Message(mod.shared_edit, user=user)  # mentions a DIFFERENT unit than the target

        await user.reflect(msg, result)

        assert len(partner.sent) == 2

    @pytest.mark.asyncio
    async def test_result_already_covering_the_targeted_element_is_not_duplicated(self, make_user):
        user = make_user("home")
        mod = user.screen_module
        partner = FakeReflectionUser(mod)
        user.reflections = [user, partner]
        msg = received("Root", "Plain", "changed", "x")
        result = Message(mod.plain_edit, user=user)

        await user.reflect(msg, result)

        assert len(partner.sent) == 1


# =============================================================================
# progress
# =============================================================================

class TestProgress:
    @pytest.mark.asyncio
    async def test_is_a_noop_while_testing(self, make_user):
        user = make_user(session=testdir)
        await user.progress("50%")  # must not raise (no self.send needed)

    @pytest.mark.asyncio
    async def test_sends_a_progress_message_when_not_testing(self, make_user, wire_send):
        user = make_user()
        send = wire_send(user)
        user.reflections = []

        await user.progress("50%")

        assert len(send.sent) == 1
        assert send.sent[0].value == "50%"

    @pytest.mark.asyncio
    async def test_none_value_closes_the_progress_window(self, make_user, wire_send):
        # progress() does TypeMessage('progress', str(value), ...) -- the
        # wire value for "hide" is the literal string 'None', not Python's
        # None.
        user = make_user()
        send = wire_send(user)
        user.reflections = []

        await user.progress(None)

        assert send.sent[0].value == "None"

    @pytest.mark.asyncio
    async def test_notifies_the_monitor_when_active(self, make_user, wire_send, monkeypatch):
        import unisi.users as users_module
        user = make_user()
        wire_send(user)
        user.reflections = []
        notified = []
        async def fake_notify(kind, session, message):
            notified.append((kind, session))
        monkeypatch.setattr(users_module, "notify_monitor", fake_notify)

        await user.progress("50%")

        assert notified == [("e", user.session)]


# =============================================================================
# delete
# =============================================================================

class TestDelete:
    @pytest.mark.asyncio
    async def test_removes_itself_from_sessions(self, make_user):
        user = make_user(session=testdir)
        Unishare.sessions[user.session] = user
        await user.delete()
        assert user.session not in Unishare.sessions

    @pytest.mark.asyncio
    async def test_missing_from_sessions_does_not_raise(self, make_user):
        user = make_user(session=testdir)
        Unishare.sessions.pop(user.session, None)
        await user.delete()

    @pytest.mark.asyncio
    async def test_stops_voice_if_present(self, make_user):
        user = make_user(session=testdir)
        stopped = []
        user.voice = type("FakeVoice", (), {"stop": lambda self: stopped.append(True)})()
        await user.delete()
        assert stopped == [True]

    @pytest.mark.asyncio
    async def test_two_member_reflections_are_cleared_entirely(self, make_user):
        user = make_user(session=testdir)
        partner = FakeReflectionUser()
        user.reflections = [user, partner]
        await user.delete()
        assert user.reflections == []

    @pytest.mark.asyncio
    async def test_larger_reflections_only_remove_self(self, make_user):
        user = make_user(session=testdir)
        p1, p2 = FakeReflectionUser(), FakeReflectionUser()
        shared = [user, p1, p2]
        user.reflections = shared
        await user.delete()
        assert shared == [p1, p2]

    @pytest.mark.asyncio
    async def test_notifies_the_monitor_when_active(self, make_user, monkeypatch):
        import unisi.users as users_module
        user = make_user(session=testdir)
        notified = []
        async def fake_notify(kind, session, message):
            notified.append((kind, session))
        monkeypatch.setattr(users_module, "notify_monitor", fake_notify)

        await user.delete()

        assert notified == [("-", testdir)]

    @pytest.mark.asyncio
    async def test_logs_disconnect_when_config_share_enabled(self, make_user, monkeypatch, caplog):
        import config
        user = make_user(session=testdir)
        monkeypatch.setattr(config, "share", True, raising=False)

        with caplog.at_level("INFO"):
            await user.delete()

        assert any("disconnected" in r.getMessage() for r in caplog.records)


# =============================================================================
# activate_session
# =============================================================================

class TestActivateSession:
    def test_updates_session_id(self, make_user):
        user = make_user(session=testdir)
        user.activate_session("new-session-id")
        assert user.session == "new-session-id"

    def test_leaves_testing_mode(self, make_user):
        user = make_user(session=testdir)
        assert user.testing is True
        user.activate_session("new-session-id")
        assert user.testing is False

    def test_resets_db(self, make_user):
        user = make_user(session=testdir)
        user.db = "stale-connection"
        user.activate_session("new-session-id")
        assert user.db is None

    def test_does_not_itself_register_in_unishare_sessions(self, make_user):
        user = make_user(session=testdir)
        user.activate_session("new-session-id")
        assert "new-session-id" not in Unishare.sessions

    def test_leaving_testing_mode_restores_persisted_screens(self, make_user):
        user = make_user(session=testdir)
        module = user.ensure_screen("Home")
        user.screen_module = module
        calls = []
        user._restore_persist_screen = lambda screen_module: calls.append(screen_module)

        user.activate_session("new-session-id")

        assert calls == [module]

    def test_already_non_testing_session_does_not_restore_again(self, make_user):
        user = make_user()  # already a real, non-testdir session
        calls = []
        user._restore_persist_screen = lambda screen_module: calls.append(screen_module)

        user.activate_session("another-real-session")

        assert calls == []


# =============================================================================
# update_menu
# =============================================================================

class TestUpdateMenu:
    def test_builds_name_icon_pairs_from_the_registry(self, make_user):
        user = make_user("home")
        names = {info.name for info in user.screen_registry}
        assert names == {"Home", "Other"}

        user.update_menu()

        menu_names = {name for name, icon in user.screen_module.screen.menu}
        assert menu_names == {"Home", "Other"}


# =============================================================================
# monitor / log
# =============================================================================

class TestMonitor:
    def test_noop_when_config_share_is_false(self, make_user, monkeypatch, caplog):
        import config
        user = make_user(session=testdir)
        monkeypatch.setattr(config, "share", False, raising=False)
        with caplog.at_level("INFO"):
            user.monitor("some-session")
        assert caplog.records == []

    def test_noop_for_the_testdir_session_even_with_share_enabled(self, make_user, monkeypatch, caplog):
        import config
        user = make_user(session=testdir)
        monkeypatch.setattr(config, "share", True, raising=False)
        with caplog.at_level("INFO"):
            user.monitor(testdir)
        assert caplog.records == []

    def test_logs_when_share_enabled_and_not_testdir(self, make_user, monkeypatch, caplog):
        import config
        user = make_user(session=testdir)
        monkeypatch.setattr(config, "share", True, raising=False)
        with caplog.at_level("INFO"):
            user.monitor("real-session-id")
        assert any("real-session-id" in r.getMessage() for r in caplog.records)


class TestLog:
    def test_error_goes_to_logging_error(self, make_user, caplog):
        user = make_user(session=testdir)
        with caplog.at_level("DEBUG"):
            user.log("something broke")
        assert caplog.records[-1].levelname == "ERROR"

    def test_message_includes_session_and_screen_context(self, make_user, caplog):
        user = make_user("home")
        with caplog.at_level("DEBUG"):
            user.log("context check")
        text = caplog.records[-1].getMessage()
        assert user.session in text
        assert "Home" in text


# =============================================================================
# run_process / sync_send
# =============================================================================

class TestRunProcess:
    @pytest.mark.asyncio
    async def test_delegates_to_run_external_process(self, make_user, monkeypatch):
        import unisi.users as users_module
        user = make_user(session=testdir)
        calls = []
        async def fake_run_external(task, *args, progress_callback=None, **kwargs):
            calls.append((task, args, progress_callback, kwargs))
            return "RESULT"
        monkeypatch.setattr(users_module, "run_external_process", fake_run_external)

        def my_task(x):
            return x * 2
        result = await user.run_process(my_task, 21, extra="kw")

        assert result == "RESULT"
        assert calls == [(my_task, (21,), None, {"extra": "kw"})]

    @pytest.mark.asyncio
    async def test_progress_callback_correctly_awaits_and_calls_through(self, make_user, monkeypatch):
        """
        Regression test for two fixed bugs. (1) new_callback's own
        `asyncio.gather(...)` was missing `await`, so the wrapped
        progress_callback (and the monitor notification) were merely
        scheduled, never actually guaranteed to run. (2) fixing that
        immediately surfaced a second, previously-invisible bug: new_callback
        closed over the local variable `progress_callback`, which the very
        next line reassigned to new_callback itself -- so calling the
        wrapped callback found itself and recursed forever once the gather
        was actually awaited. Fixed by capturing the original callback in
        its own variable before the reassignment.
        """
        import unisi.users as users_module
        user = make_user(session=testdir)

        async def fake_run_external(task, *args, progress_callback=None, **kwargs):
            await progress_callback("mid")
            return None
        monkeypatch.setattr(users_module, "run_external_process", fake_run_external)

        notified = []
        async def fake_notify(kind, session, message):
            notified.append(kind)
        monkeypatch.setattr(users_module, "notify_monitor", fake_notify)

        progress_calls = []
        async def my_progress(v):
            progress_calls.append(v)

        await user.run_process(lambda: None, progress_callback=my_progress)

        assert progress_calls == ["mid"]
        assert notified == ["e"]

    @pytest.mark.asyncio
    async def test_self_progress_is_never_wrapped(self, make_user, monkeypatch):
        import unisi.users as users_module
        user = make_user(session=testdir)
        calls = []
        async def fake_run_external(task, *args, progress_callback=None, **kwargs):
            calls.append(progress_callback)
            return None
        monkeypatch.setattr(users_module, "run_external_process", fake_run_external)
        async def fake_notify(kind, session, message):
            pass
        monkeypatch.setattr(users_module, "notify_monitor", fake_notify)

        await user.run_process(lambda: None, progress_callback=user.progress)

        assert calls == [user.progress]


class TestSyncSend:
    def test_runs_send_synchronously(self, make_user, wire_send):
        user = make_user(session=testdir)
        send = wire_send(user)

        user.sync_send("hello")

        assert send.sent == ["hello"]


# =============================================================================
# compile_screen (User's own testing-mode override)
# =============================================================================

class TestCompileScreen:
    def test_clean_screen_gets_reactivity_and_is_registered(self, make_user, real_screens_dir):
        (real_screens_dir / "valid.py").write_text(
            "from unisi import Text, Block\n"
            "name = 'Valid'\n"
            "blocks = [Block('main', Text('hello'))]\n"
        )
        user = make_user(session=testdir)

        module = user.compile_screen("valid.py")

        assert module.screen.name == "Valid"
        assert module.screen._mark_changed is not None
        from unisi.users import User
        assert any(info.name == "Valid" for info in User.screen_registry)

    def test_screen_with_validation_errors_skips_reactivity_while_testing(self, make_user, real_screens_dir, capsys):
        (real_screens_dir / "noname.py").write_text(
            "from unisi import Text, Block\n"
            "blocks = [Block('main', Text('hello'))]\n"
        )
        user = make_user(session=testdir)

        module = user.compile_screen("noname.py")

        assert module.screen._mark_changed is None
        assert "does not contain name" in capsys.readouterr().out

    def test_same_broken_screen_still_gets_reactivity_when_not_testing(self, real_screens_dir, capsys):
        (real_screens_dir / "noname.py").write_text(
            "from unisi import Text, Block\n"
            "blocks = [Block('main', Text('hello'))]\n"
        )
        from unisi.users import User
        user = User("a-real-session-for-compile-test")

        module = user.compile_screen("noname.py")

        assert module.screen._mark_changed is not None


# =============================================================================
# calc_dbsharing / sync_dbupdates
# =============================================================================

class TestCalcDbsharing:
    def test_maps_element_id_to_screen_and_location(self, make_user):
        from unisi.dbunits import dbshare
        dbshare.clear()
        user = make_user("home")

        user.calc_dbsharing()

        assert dbshare[4242]["Home"] == [{"element": "Shared", "block": "Root"}]

    def test_elements_without_id_are_ignored(self, make_user):
        from unisi.dbunits import dbshare
        dbshare.clear()
        user = make_user("home")

        user.calc_dbsharing()

        assert 4242 in dbshare  # shared_edit has one
        # plain_edit/save_button (no id=) never show up as keys anywhere
        for screens in dbshare.values():
            for entries in screens.values():
                assert all(e["element"] != "Plain" for e in entries)


class TestSyncDbupdates:
    @pytest.mark.asyncio
    async def test_broadcasts_update_to_all_sharing_users_by_default(self, make_user, wire_send):
        from unisi.dbunits import dbshare, dbupdates
        dbshare.clear()
        dbupdates.clear()
        user = make_user("home")
        user.calc_dbsharing()
        send = wire_send(user)
        Unishare.sessions[user.session] = user

        dbupdates[4242].append({"value": "new", "exclude": False})
        await user.sync_dbupdates()

        assert send.sent == [{"value": "new", "exclude": False, "element": "Shared", "block": "Root"}]
        assert dict(dbupdates) == {}

    @pytest.mark.asyncio
    async def test_exclude_true_skips_the_originating_user(self, make_user, wire_send):
        from unisi.dbunits import dbshare, dbupdates
        dbshare.clear()
        dbupdates.clear()
        user = make_user("home")
        user.calc_dbsharing()
        send = wire_send(user)
        Unishare.sessions[user.session] = user

        dbupdates[4242].append({"value": "new", "exclude": True})
        await user.sync_dbupdates()

        assert send.sent == []


# =============================================================================
# User.init_user()
# =============================================================================

class TestInitUser:
    def test_returns_a_testing_user(self, real_screens_dir):
        (real_screens_dir / "a.py").write_text("from unisi import Block\nname = 'A'\nblocks = []\n")
        from unisi.users import User
        user = User.init_user()
        assert user.testing is True
        assert user.session == testdir

    def test_prepares_every_registered_screen_once(self, real_screens_dir):
        # 'a' sorts/loads first and becomes the auto-loaded default during
        # User(testdir) construction itself -- already_prepared starts
        # seeded with whatever was auto-loaded, so putting prepare() on 'b'
        # instead is what actually exercises init_user()'s own loop body.
        (real_screens_dir / "a.py").write_text("from unisi import Block\nname = 'A'\nblocks = []\n")
        (real_screens_dir / "b.py").write_text(
            "from unisi import Block\n"
            "name = 'B'\n"
            "blocks = []\n"
            "prepared = []\n"
            "def prepare():\n"
            "    prepared.append(1)\n"
        )
        from unisi.users import User

        user = User.init_user()

        names = {info.name for info in user.screen_registry}
        assert names == {"A", "B"}
        b_module = user.ensure_screen("B")
        assert b_module.prepared == [1]

    def test_computes_dbsharing_across_all_screens(self, real_screens_dir):
        from unisi.dbunits import dbshare
        (real_screens_dir / "a.py").write_text(
            "from unisi import Block, Edit\n"
            "name = 'A'\n"
            "blocks = [Block('main', Edit('Shared', 'x', id=7))]\n"
        )
        dbshare.clear()
        from unisi.users import User

        User.init_user()

        assert 7 in dbshare
        assert dbshare[7]["A"] == [{"element": "Shared", "block": "main"}]
