"""
Tests for the broader users.py request/response machinery: find_path,
register_changed_unit's echo-suppression rules, and prepare_result's
handling of the various shapes `raw` can take (None, a Unit, a list, True).
"""
import pytest

from unisi import Edit
from unisi.common import ReceivedMessage


def test_find_path_is_leaf_first(make_user):
    user = make_user("positional")
    mod = user.screen_module
    assert user.find_path(mod.inner) == ["Inner", "Plain block", "Root"]


def test_find_path_for_a_direct_screen_child(make_user):
    user = make_user("positional")
    assert user.find_path(user.screen_module.flagged) == ["Flagged", "Root"]


def test_find_path_for_a_block_itself(make_user):
    user = make_user("positional")
    mod = user.screen_module
    assert user.find_path(mod.plain_block) == ["Plain block", "Root"]


def test_find_path_none_for_an_unrelated_unit(make_user):
    user = make_user("positional")
    stray = Edit("Stray", "nowhere")
    assert user.find_path(stray) is None


class TestRegisterChangedUnitEchoSuppression:
    def test_noop_with_no_active_message(self, make_user):
        user = make_user("positional")
        unit = user.screen_module.flagged
        user.last_message = None

        user.register_changed_unit(unit)

        assert unit not in user.changed_units

    def test_adds_unit_when_message_is_about_something_else(self, make_user):
        user = make_user("positional")
        unit = user.screen_module.flagged
        user.last_message = ReceivedMessage(
            {"block": "Root", "element": "some other button", "event": "changed", "value": None}
        )

        user.register_changed_unit(unit)

        assert unit in user.changed_units

    def test_suppresses_exact_echo_of_the_incoming_value_change(self, make_user):
        # if the message IS this unit's own "changed" event with this exact
        # value, re-adding it to changed_units would just echo back what the
        # client already knows it just sent.
        user = make_user("positional")
        unit = user.screen_module.flagged
        user.last_message = ReceivedMessage(
            {"block": "Root", "element": "Flagged", "event": "changed", "value": "same"}
        )

        user.register_changed_unit(unit, "value", "same")

        assert unit not in user.changed_units

    def test_does_not_suppress_when_the_value_differs_from_the_message(self, make_user):
        user = make_user("positional")
        unit = user.screen_module.flagged
        user.last_message = ReceivedMessage(
            {"block": "Root", "element": "Flagged", "event": "changed", "value": "typed value"}
        )

        # e.g. a handler normalized/overrode what the client sent
        user.register_changed_unit(unit, "value", "normalized value")

        assert unit in user.changed_units

    def test_modify_event_on_the_same_element_and_block_is_suppressed(self, make_user):
        user = make_user("positional")
        unit = user.screen_module.flagged
        # the 'modify' echo-suppression check compares m.block against
        # strpath(find_path(unit)), which -- unlike the block/element split
        # used elsewhere -- includes the element's own name (see find_path:
        # it returns [unit.name, *ancestors]), hence "Flagged@Root" here,
        # not just "Root".
        from unisi.common import strpath

        user.last_message = ReceivedMessage(
            {
                "block": strpath(user.find_path(unit)),
                "element": "Flagged",
                "event": "modify",
                "value": "partial",
            }
        )

        result = user.register_changed_unit(unit)

        assert result is False
        assert unit not in user.changed_units

    def test_modify_event_on_a_different_element_is_not_suppressed(self, make_user):
        user = make_user("positional")
        unit = user.screen_module.flagged
        user.last_message = ReceivedMessage(
            {"block": "Root", "element": "some other field", "event": "modify", "value": "partial"}
        )

        user.register_changed_unit(unit)

        assert unit in user.changed_units


class TestPrepareResultRawShapes:
    def test_none_with_no_changes_stays_none(self, make_user):
        user = make_user("positional")
        assert user.prepare_result(None) is None

    def test_none_with_pending_changes_becomes_a_message(self, make_user):
        from unisi.common import Message

        user = make_user("positional")
        unit = user.screen_module.flagged
        user.last_message = ReceivedMessage(
            {"block": "Root", "element": "trigger", "event": "changed", "value": None}
        )
        user.register_changed_unit(unit)

        result = user.prepare_result(None)

        assert isinstance(result, Message)
        assert any(u["data"] is unit for u in result.updates)

    def test_a_single_unit_becomes_a_message_containing_it(self, make_user):
        from unisi.common import Message

        user = make_user("positional")
        unit = user.screen_module.flagged

        result = user.prepare_result(unit)

        assert isinstance(result, Message)
        assert any(u["data"] is unit for u in result.updates)

    def test_a_list_of_units_becomes_a_message_containing_all_of_them(self, make_user):
        from unisi.common import Message

        user = make_user("positional")
        mod = user.screen_module

        result = user.prepare_result([mod.flagged, mod.plain])

        assert isinstance(result, Message)
        updated = [u["data"] for u in result.updates]
        assert mod.flagged in updated
        assert mod.plain in updated

    def test_true_returns_the_whole_screen(self, make_user):
        user = make_user("positional")
        result = user.prepare_result(True)
        assert result is user.screen

    def test_a_message_object_passes_through_with_paths_filled(self, make_user):
        from unisi.common import Message

        user = make_user("positional")
        unit = user.screen_module.flagged
        msg = Message(user=user)

        result = user.prepare_result(msg)

        assert result is msg


def test_find_element_resolves_a_top_level_unit(make_user):
    user = make_user("positional")
    msg = ReceivedMessage({"block": "Root", "element": "Flagged", "event": "changed", "value": "x"})
    assert user.find_element(msg) is user.screen_module.flagged


def test_find_element_resolves_a_nested_unit(make_user):
    user = make_user("positional")
    msg = ReceivedMessage(
        {"block": "Plain block@Root", "element": "Plain", "event": "changed", "value": "x"}
    )
    assert user.find_element(msg) is user.screen_module.plain


def test_find_element_returns_none_for_an_unknown_element(make_user):
    user = make_user("positional")
    msg = ReceivedMessage(
        {"block": "Root", "element": "Does not exist", "event": "changed", "value": "x"}
    )
    assert user.find_element(msg) is None
