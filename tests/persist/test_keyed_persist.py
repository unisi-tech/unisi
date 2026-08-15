"""
Tests for keyed persist (persist=<function>, spec.md 13.2): a unit's key is
recomputed every request; when it changes, a saved value for the new key is
restored (or the unit is left as whatever a handler put there, with
active=False); when the key is unchanged but the unit was touched, its
current value is saved under that key.
"""
import pytest


@pytest.mark.asyncio
async def test_editing_after_the_key_is_established_saves_under_that_key(make_user, deliver):
    user = make_user("keyed")
    # a keyed unit's very first-ever key evaluation always takes the
    # restore branch, even if it's also touched in that same round (see
    # test_first_key_evaluation_does_not_save_even_if_touched below) --
    # touch something unrelated first so `single_key_field`'s key ("A", from
    # Selector's default) is already established before the real edit.
    await deliver(user, "Root", "Selector", "changed", "A")

    await deliver(user, "Root", "Single key field", "changed", "value for A")

    ns, path = user.persist_location(user.screen_module.single_key_field)
    saved = user.get_objects(ns, path, "A")
    assert saved.get("A", {}).get("value") == "value for A"


@pytest.mark.asyncio
async def test_context_key_for_a_single_value_has_no_json_wrapping(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "x")

    ns, path = user.persist_location(user.screen_module.single_key_field)
    contexts = user.get_contexts(ns, path, "..")
    assert contexts == ["A"]  # not '["A"]'


@pytest.mark.asyncio
async def test_switching_to_a_never_saved_key_shows_nothing_and_active_false(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "value for A")

    # switch to a fresh key that has nothing saved yet
    await deliver(user, "Root", "Selector", "changed", "B")

    field = user.screen_module.single_key_field
    assert field.active is False


@pytest.mark.asyncio
async def test_switching_back_to_a_saved_key_restores_the_value(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "Selector", "changed", "A")
    await deliver(user, "Root", "Single key field", "changed", "value for A")
    await deliver(user, "Root", "Selector", "changed", "B")  # away

    await deliver(user, "Root", "Selector", "changed", "A")  # back

    field = user.screen_module.single_key_field
    assert field.value == "value for A"
    assert field.active is True


@pytest.mark.asyncio
async def test_multi_value_key_is_comma_joined(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "City", "changed", "London")
    await deliver(user, "Root", "Zip", "changed", "10001")
    await deliver(user, "Root", "Multi key field", "changed", "note for London 10001")

    ns, path = user.persist_location(user.screen_module.multi_key_field)
    saved = user.get_objects(ns, path, "London,10001")
    assert saved.get("London,10001", {}).get("value") == "note for London 10001"


@pytest.mark.asyncio
async def test_multi_value_prefix_template_search(make_user, deliver):
    user = make_user("keyed")
    await deliver(user, "Root", "City", "changed", "London")
    await deliver(user, "Root", "Zip", "changed", "10001")
    await deliver(user, "Root", "Multi key field", "changed", "one")
    await deliver(user, "Root", "City", "changed", "London")
    await deliver(user, "Root", "Zip", "changed", "20002")
    await deliver(user, "Root", "Multi key field", "changed", "two")
    await deliver(user, "Root", "City", "changed", "Paris")
    await deliver(user, "Root", "Zip", "changed", "30003")
    await deliver(user, "Root", "Multi key field", "changed", "three")

    ns, path = user.persist_location(user.screen_module.multi_key_field)
    london_only = user.get_contexts(ns, path, "London,..")
    assert set(london_only) == {"London,10001", "London,20002"}


@pytest.mark.asyncio
async def test_first_key_evaluation_does_not_save_even_if_touched(make_user, deliver):
    # Documents a real, narrow edge case (found while investigating the
    # progress()/persist-timing issue, unrelated to it): a keyed unit's
    # very first-ever key evaluation always takes the "key changed ->
    # restore" branch, even if the unit was ALSO edited in that same
    # request -- restore always wins over save on the first pass, so the
    # edit is not persisted this particular round (though it IS still
    # applied live -- nothing is lost from the user's perspective, it's
    # simply not saved on disk yet). This test exists so a future change
    # to sync_keyed_persist's key-changed detection doesn't silently
    # alter this without someone noticing.
    user = make_user("keyed")

    # Selector is already "A" by default, so `single_key_field`'s key
    # function has never been evaluated before this exact message -- editing
    # it here is genuinely the first-ever pass for its key.
    await deliver(user, "Root", "Single key field", "changed", "first ever edit")

    assert user.screen_module.single_key_field.value == "first ever edit"  # applied live
    ns, path = user.persist_location(user.screen_module.single_key_field)
    assert user.get_objects(ns, path, "A") == {}  # but not saved yet

    # a later, separate edit (key already established) saves normally
    await deliver(user, "Root", "Single key field", "changed", "second edit")
    assert user.get_objects(ns, path, "A").get("A", {}).get("value") == "second edit"


@pytest.mark.asyncio
async def test_keyed_value_survives_a_reconnect(make_user, deliver, wire_send):
    session = "keyed-reconnect"
    user1 = make_user("keyed", session=session)
    await deliver(user1, "Root", "Selector", "changed", "A")
    await deliver(user1, "Root", "Single key field", "changed", "persisted value")

    user2 = make_user("keyed", session=session)
    # Unlike positional persist (restored automatically as part of loading
    # the screen -- see test_positional_persist.py's reconnect tests), keyed
    # persist is only ever restored by sync_keyed_persist(), which only runs
    # inside prepare_result -- i.e. only once something is actually sent.
    # A real connection triggers this itself with its very first send(True)
    # right after connecting (see server.py's websocket_handler); simulate
    # that same initial send here.
    send = wire_send(user2)
    await send(True)

    # Selector defaults to "A" again on a fresh load, so the key matches and
    # the saved value comes back as part of that first sync_keyed_persist pass.
    assert user2.screen_module.single_key_field.value == "persisted value"
    assert user2.screen_module.single_key_field.active is True


@pytest.mark.asyncio
async def test_keyed_restore_needs_a_prepare_result_pass_unlike_positional(make_user, wire_send):
    # The asymmetry test_keyed_value_survives_a_reconnect's fix works around:
    # right after construction, screen loading alone has already restored
    # positional persist=True units (see _finish_loaded_screen /
    # _restore_persist_screen), but a keyed unit is untouched until
    # sync_keyed_persist actually runs at least once.
    session = "keyed-restore-timing"
    user1 = make_user("keyed", session=session)
    from unisi.common import ReceivedMessage
    import asyncio as _asyncio

    async def _send(res, persist=True):
        return user1.prepare_result(res, persist=persist)

    user1.send = _send
    msg = ReceivedMessage({"block": "Root", "element": "Selector", "event": "changed", "value": "A"})
    await user1.result4message(msg)
    await user1.send(None)
    msg2 = ReceivedMessage({"block": "Root", "element": "Single key field", "event": "changed", "value": "kept"})
    await user1.result4message(msg2)
    await user1.send(None)

    user2 = make_user("keyed", session=session)
    # not restored yet -- sync_keyed_persist hasn't run for user2 at all
    assert user2.screen_module.single_key_field.value == ""

    send = wire_send(user2)
    await send(True)  # first prepare_result pass -- NOW it restores
    assert user2.screen_module.single_key_field.value == "kept"
