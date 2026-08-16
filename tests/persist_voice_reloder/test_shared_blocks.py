"""
Tests for persistence of units living in a blocks/ module shared across
screens (spec.md 13.6): identity is anchored to the block's own module
('@dotted.module.name'), not to whichever screen currently hosts it, and
persist=True on the block cascades automatically to its children.
"""
import pytest


@pytest.mark.asyncio
async def test_persist_true_on_a_shared_block_cascades_to_its_children(make_user, deliver):
    user = make_user("shared_host")
    shared_mod = user.modules["blocks.shared"]

    await deliver(user, "Auto shared block", "Auto edit", "changed", "cascaded save")

    ns, path = user.persist_location(shared_mod.auto_edit)
    assert user.get_objects(ns, path, "")[""]["value"] == "cascaded save"


def test_shared_namespace_is_anchored_to_the_module_not_the_screen(make_user):
    user = make_user("shared_host")
    shared_mod = user.modules["blocks.shared"]

    ns, path = user.persist_location(shared_mod.auto_edit)

    assert ns == "@blocks.shared"


@pytest.mark.asyncio
async def test_shared_value_survives_a_reconnect(make_user, deliver):
    session = "shared-reconnect"
    user1 = make_user("shared_host", session=session)
    await deliver(user1, "Auto shared block", "Auto edit", "changed", "saved from session one")

    user2 = make_user("shared_host", session=session)
    shared_mod2 = user2.modules["blocks.shared"]
    assert shared_mod2.auto_edit.value == "saved from session one"


@pytest.mark.asyncio
async def test_identity_is_stable_regardless_of_which_screen_is_current(make_user, deliver):
    user = make_user("shared_host")
    shared_mod = user.modules["blocks.shared"]

    loc_on_shared_host = user.persist_location(shared_mod.auto_edit)

    other = user.ensure_screen("positional")
    user.screen_module = other
    loc_from_elsewhere = user.persist_location(shared_mod.auto_edit)

    assert loc_on_shared_host == loc_from_elsewhere


def test_manual_shared_unit_is_not_auto_persisted(make_user):
    # manual_block/manual_edit carry no persist flag at all in the fixture --
    # baseline for persist_units targeting it on demand (see
    # test_persist_units.py's shared-block tests).
    user = make_user("shared_host")
    shared_mod = user.modules["blocks.shared"]

    ns, path = user.persist_location(shared_mod.manual_edit)
    shared_mod.manual_edit.value = "edited but not flagged"

    assert user.get_objects(ns, path, "") == {}
