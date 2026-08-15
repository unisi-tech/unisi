"""
Tests for positional persist (persist=True, spec.md 13.1): a unit's state is
saved under (namespace, path, context_key='') whenever it changes, and
restored onto it the next time its screen is loaded.
"""
import pytest


@pytest.mark.asyncio
async def test_changing_a_flagged_unit_persists_it(make_user, deliver):
    user = make_user("positional")
    await deliver(user, "Root", "Flagged", "changed", "saved value")

    ns, path = user.persist_location(user.screen_module.flagged)
    saved = user.get_objects(ns, path, "")
    assert saved.get("", {}).get("value") == "saved value"


@pytest.mark.asyncio
async def test_unflagged_unit_is_not_auto_saved(make_user, deliver):
    # `plain` has no persist=... at all -- an ordinary edit must not create
    # a row for it. (This is the baseline persist_units/restore_units exists
    # to override on demand -- see test_persist_units.py.)
    user = make_user("positional")
    await deliver(user, "Plain block@Root", "Plain", "changed", "just a normal edit")
    assert user.screen_module.plain.value == "just a normal edit"  # the edit did apply live

    ns, path = user.persist_location(user.screen_module.plain)
    assert user.get_objects(ns, path, "") == {}


@pytest.mark.asyncio
async def test_saved_value_survives_a_reconnect(make_user, deliver):
    session = "positional-reconnect"
    user1 = make_user("positional", session=session)
    await deliver(user1, "Root", "Flagged", "changed", "value from session one")

    # simulate the user reloading the page: a brand new User, same session id
    user2 = make_user("positional", session=session)
    assert user2.screen_module.flagged.value == "value from session one"


@pytest.mark.asyncio
async def test_unflagged_unit_does_not_survive_a_reconnect(make_user, deliver):
    session = "positional-reconnect-plain"
    user1 = make_user("positional", session=session)
    await deliver(user1, "Plain block@Root", "Plain", "changed", "not actually saved")
    assert user1.screen_module.plain.value == "not actually saved"  # applied live in session one

    user2 = make_user("positional", session=session)
    # back to whatever the screen module defines as the default, since
    # nothing was ever persisted for it
    assert user2.screen_module.plain.value == "unflagged"


@pytest.mark.asyncio
async def test_repeated_identical_value_does_not_error_or_change_result(make_user, deliver):
    user = make_user("positional")
    await deliver(user, "Root", "Flagged", "changed", "same")
    await deliver(user, "Root", "Flagged", "changed", "same")

    ns, path = user.persist_location(user.screen_module.flagged)
    assert user.get_objects(ns, path, "").get("", {}).get("value") == "same"


@pytest.mark.asyncio
async def test_a_persist_true_block_saves_its_whole_subtree(make_user, deliver):
    # positional persist=True on a *container* saves/restores it as a whole,
    # unlike keyed persist which never targets a container (see spec 13.2).
    # `plain_block` has no persist flag in the fixture screen itself, so it's
    # flipped on here for just this test, then a real message touches one of
    # its children -- message.block addresses a nested element leaf-first,
    # innermost container first, root last (same convention as persist paths
    # -- see find_element and _unit_path_key), so "Plain block@Root" reaches
    # `plain`, which lives in "Plain block", which lives in "Root".
    user = make_user("positional")
    user.screen_module.plain_block.persist = True

    await deliver(user, "Plain block@Root", "Plain", "changed", "child one")

    ns, path = user.persist_location(user.screen_module.plain_block)
    saved = user.get_objects(ns, path, "")
    assert saved != {}
    children = {c["name"]: c for c in saved[""]["value"]}
    assert children["Plain"]["value"] == "child one"
    assert children["Inner"]["value"] == "inner-default"  # untouched sibling, saved too


@pytest.mark.asyncio
async def test_persist_location_is_none_for_a_completely_unrelated_unit(make_user):
    from unisi import Edit

    user = make_user("positional")
    stray = Edit("Stray", "nowhere")
    assert user.persist_location(stray) is None


@pytest.mark.asyncio
async def test_get_key_style_read_of_a_positional_row_via_get_objects(make_user, deliver):
    # get_objects/get_contexts double as a generic way to inspect ANY row,
    # including a positional persist=True save (context_key='').
    user = make_user("positional")
    await deliver(user, "Root", "Flagged", "changed", "inspect me")

    ns, path = user.persist_location(user.screen_module.flagged)
    assert user.get_contexts(ns, path, "") == [""]
