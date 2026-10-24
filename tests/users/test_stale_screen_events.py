"""
Events produced on a screen the user has already left (User.is_stale).

A message path names its target only by element and block names, so it means something
only on the screen the client displayed when it sent the message. The client keeps showing
the old screen until the server's full-screen reply arrives, and the server handles the
messages one by one: a screen switch and the events the user still makes on the old screen
queue up behind a long handler (one that reports user.progress(), say). When they are
processed the server is already on the new screen, where the same path names another
element -- or a block, and `block.value = True` then raised

    AttributeError: 'bool' object has no attribute 'set_reactivity'

in Block.set_reactivity and closed the whole connection -- or nothing.

The client therefore sends the name of the screen an event was produced on in the optional
`screen` field of the message (docs/protocol.md, 4.1) and the server ignores the event when
the session is on another screen by then. Same real-User, fixtures_app/ approach as
test_users.py: screens "Home" and "Other" (both have a block named "Root").
"""
import pytest

from unisi.common import Message, ReceivedMessage
from unisi.containers import Block
from unisi.units import Edit


def produced_on(screen, block, element, event, value=None):
    """An element event as a client sends it: `screen` is the screen it displayed."""
    path = [element, *block.split('@')] if element else [block]
    return ReceivedMessage({"path": path, "event": event, "value": value, "screen": screen})


def untagged(block, element, event, value=None):
    """The same event from a client that does not send `screen` (an older client)."""
    path = [element, *block.split('@')] if element else [block]
    return ReceivedMessage({"path": path, "event": event, "value": value})


def switch_to(screen_name):
    return ReceivedMessage({"path": ["root"], "event": "changed", "value": screen_name})


async def leave_home_for_other(user):
    """What the websocket loop does with a queued screen switch: returns the Other module."""
    assert await user.result4message(switch_to("Other")) is True
    assert user.screen.name == "Other"
    return user.screen_module


class TestIsStale:
    def test_an_event_without_the_field_is_never_stale(self, make_user):
        user = make_user("home")
        assert user.is_stale(untagged("Root", "Plain", "changed", "x")) is False

    def test_an_event_produced_on_the_current_screen_is_not_stale(self, make_user):
        user = make_user("home")
        assert user.is_stale(produced_on("Home", "Root", "Plain", "changed", "x")) is False

    def test_an_event_produced_on_another_screen_is_stale(self, make_user):
        user = make_user("home")
        assert user.is_stale(produced_on("Other", "Root", "Plain", "changed", "x")) is True

    def test_an_empty_field_is_the_same_as_no_field(self, make_user):
        user = make_user("home")
        assert user.is_stale(produced_on("", "Root", "Plain", "changed", "x")) is False

    @pytest.mark.asyncio
    async def test_staleness_follows_the_current_screen(self, make_user):
        user = make_user("home")
        message = produced_on("Home", "Root", "Plain", "changed", "x")
        assert user.is_stale(message) is False
        await leave_home_for_other(user)
        assert user.is_stale(message) is True
        assert await user.result4message(switch_to("Home")) is True
        assert user.is_stale(message) is False


class TestEventsOfTheScreenTheUserLeft:
    @pytest.mark.asyncio
    async def test_a_path_naming_a_block_on_the_new_screen_does_not_crash(self, make_user):
        """The reported traceback: Switch-like `changed` events carry a bool, and on the new
        screen the same path named a block."""
        user = make_user("home")
        other = await leave_home_for_other(user)
        nested = Block("Plain", Edit("Nested", "n"))              # on Other, ['Plain', 'Root'] is a block
        other.screen.blocks[0].value = [nested]

        result = await user.result4message(produced_on("Home", "Root", "Plain", "changed", True))

        assert result is None
        assert isinstance(nested.value, list) and len(nested.value) == 1

    @pytest.mark.asyncio
    async def test_the_event_does_not_reach_a_same_named_element_of_the_new_screen(self, make_user):
        user = make_user("home")
        home = user.screen_module
        other = await leave_home_for_other(user)
        impostor = Edit("Plain", "untouched")                     # same path, other screen
        other.screen.blocks[0].value = [impostor]

        await user.result4message(produced_on("Home", "Root", "Plain", "changed", "typed on Home"))

        assert impostor.value == "untouched"
        assert home.plain_edit.value == "default"                 # the event is dropped, not redirected

    @pytest.mark.asyncio
    async def test_an_event_for_a_path_missing_on_the_new_screen_is_not_an_error(self, make_user):
        user = make_user("home")
        await leave_home_for_other(user)

        result = await user.result4message(produced_on("Home", "Root", "Plain", "changed", "x"))

        assert result is None                                     # was: Error('... does not exist!')

    @pytest.mark.asyncio
    async def test_the_handler_of_the_old_screen_is_not_called(self, make_user):
        user = make_user("home")
        home = user.screen_module
        await leave_home_for_other(user)

        await user.result4message(produced_on("Home", "Root", "Save", "changed", "x"))

        assert home.save_clicks == []

    @pytest.mark.asyncio
    async def test_the_reply_is_still_one_per_message(self, make_user, wire_send):
        """Like any message that changed nothing: a null reply, which a client waiting for
        exactly one reply per sent message relies on (docs/protocol.md, 3.2)."""
        user = make_user("home")
        send = wire_send(user)
        await user.send(await user.result4message(switch_to("Other")))   # as the websocket loop does
        assert send.sent[-1] is user.screen                              # the full screen went out
        send.sent.clear()

        result = await user.result4message(produced_on("Home", "Root", "Plain", "changed", "x"))
        await user.send(result)

        assert send.sent == [None]

    @pytest.mark.asyncio
    async def test_the_queue_of_a_busy_handler_is_processed_in_order(self, make_user):
        """[switch to Other, then an event the client still produced on Home] -- the order in
        which a long handler leaves them -- and the user comes back to Home afterwards."""
        user = make_user("home")
        home = user.screen_module
        queued = [switch_to("Other"),
                  produced_on("Home", "Root", "Plain", "changed", "typed while waiting")]

        results = [await user.result4message(message) for message in queued]

        assert results[0] is True and results[1] is None
        assert home.plain_edit.value == "default"
        assert await user.result4message(switch_to("Home")) is True
        assert user.screen_module is home

    @pytest.mark.asyncio
    async def test_the_old_screen_is_served_again_once_the_user_is_back(self, make_user):
        user = make_user("home")
        home = user.screen_module
        await leave_home_for_other(user)
        assert await user.result4message(switch_to("Home")) is True

        await user.result4message(produced_on("Home", "Root", "Plain", "changed", "typed"))

        assert home.plain_edit.value == "typed"


class TestEventsOfTheCurrentScreen:
    @pytest.mark.asyncio
    async def test_an_event_tagged_with_the_current_screen_is_processed(self, make_user):
        user = make_user("home")
        home = user.screen_module

        await user.result4message(produced_on("Home", "Root", "Plain", "changed", "typed"))

        assert home.plain_edit.value == "typed"

    @pytest.mark.asyncio
    async def test_an_event_without_the_field_is_resolved_on_the_current_screen(self, make_user):
        user = make_user("home")
        home = user.screen_module

        await user.result4message(untagged("Root", "Plain", "changed", "typed"))

        assert home.plain_edit.value == "typed"

    @pytest.mark.asyncio
    async def test_a_handler_runs_for_a_tagged_event(self, make_user):
        user = make_user("home")
        home = user.screen_module

        await user.result4message(produced_on("Home", "Root", "Save", "changed", "x"))

        assert home.save_clicks == ["x"]

    @pytest.mark.asyncio
    async def test_a_toolbar_event_is_checked_like_any_other(self, make_user):
        user = make_user("home")
        message = ReceivedMessage({"path": ["Help", "toolbar"], "event": "changed",
                                   "value": None, "screen": "Home"})
        assert user.is_stale(message) is False
        assert user.find_element(message) is user.screen_module.help_button


class TestMessagesNotAddressedToAScreenElement:
    @pytest.mark.asyncio
    async def test_a_screen_switch_ignores_the_field(self, make_user):
        """`screen` used to make the server switch screens itself (the screen named by the
        event value); it only says where an element event was produced now."""
        user = make_user("home")
        message = ReceivedMessage({"path": ["root"], "event": "changed", "value": "Other",
                                  "screen": "NoSuchScreen"})

        assert await user.result4message(message) is True

        assert user.screen.name == "Other"

    @pytest.mark.asyncio
    async def test_an_element_event_never_switches_the_screen(self, make_user):
        user = make_user("home")

        await user.result4message(produced_on("Other", "Root", "Plain", "changed", "Other"))

        assert user.screen.name == "Home"

    @pytest.mark.asyncio
    async def test_a_progress_window_command_is_never_stale(self, make_user):
        """The progress window belongs to the user, not to a screen: Cancel works on any."""
        user = make_user("home")
        pressed = []
        user.progress_handler = pressed.append
        await leave_home_for_other(user)
        message = ReceivedMessage({"path": ["Cancel", "progress"], "event": "changed",
                                   "value": None, "screen": "Home"})

        await user.result4message(message)

        assert pressed == ["Cancel"]

    @pytest.mark.asyncio
    async def test_a_voice_message_is_never_stale(self, make_user):
        user = make_user("home")
        await leave_home_for_other(user)
        message = ReceivedMessage({"path": ["voice"], "event": "listen", "value": False,
                                   "screen": "Home"})

        await user.result4message(message)

        assert user.voice is not None


class TestReflectOfAStaleEvent:
    @pytest.mark.asyncio
    async def test_a_stale_event_is_not_reflected_as_whatever_its_path_means_now(self, make_user):
        from test_users import FakeReflectionUser

        user = make_user("home")
        other = await leave_home_for_other(user)
        other.screen.blocks[0].value = [Edit("Plain", "other-screen-value")]   # same path, other screen
        partner = FakeReflectionUser(other)
        user.reflections = [user, partner]

        await user.reflect(produced_on("Home", "Root", "Plain", "changed", "x"), None)

        assert partner.sent == []

    @pytest.mark.asyncio
    async def test_a_current_event_is_reflected_as_before(self, make_user):
        from test_users import FakeReflectionUser

        user = make_user("home")
        home = user.screen_module
        partner = FakeReflectionUser(home)
        user.reflections = [user, partner]

        await user.reflect(produced_on("Home", "Root", "Plain", "changed", "x"), None)

        assert len(partner.sent) == 1
