"""
Tests for User.persist_units / User.restore_units: on-demand save/restore of
specific units regardless of whether they carry persist=... at all.
"""
import pytest

from unisi import Edit


@pytest.mark.asyncio
async def test_persist_units_saves_an_otherwise_unflagged_unit(make_user, deliver):
    user = make_user("positional")
    await deliver(user, "Plain block@Root", "Plain", "changed", "not auto-saved")

    ns, path = user.persist_location(user.screen_module.plain)
    assert user.get_objects(ns, path, "") == {}  # confirm the baseline: not auto-saved

    written = user.persist_units(user.screen_module.plain)

    assert written == [user.screen_module.plain]
    assert user.get_objects(ns, path, "").get("", {}).get("value") == "not auto-saved"


def test_persist_units_skips_a_write_when_unchanged(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain

    first = user.persist_units(plain)
    assert first == [plain]

    second = user.persist_units(plain)  # nothing changed since
    assert second == []


def test_persist_units_writes_again_after_a_real_change(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain

    user.persist_units(plain)
    plain.value = "changed"
    written = user.persist_units(plain)

    assert written == [plain]


@pytest.mark.asyncio
async def test_persist_units_uses_the_same_slot_as_positional_persist_true(make_user, deliver):
    # persist_units(unit) and persist=True must not be two separate
    # mechanisms writing to different places for the same unit.
    user = make_user("positional")
    await deliver(user, "Root", "Flagged", "changed", "auto saved")

    dedup = user.persist_units(user.screen_module.flagged)
    assert dedup == []  # already identical to what persist=True just wrote

    object.__setattr__(user.screen_module.flagged, "value", "tampered in memory")
    restored = user.restore_units(user.screen_module.flagged)
    assert restored == [user.screen_module.flagged]
    assert user.screen_module.flagged.value == "auto saved"


def test_persist_units_returns_empty_for_an_unreachable_unit(make_user):
    user = make_user("positional")
    stray = Edit("Stray", "nowhere")

    written = user.persist_units(stray)

    assert written == []


def test_persist_units_with_no_arguments_returns_empty_list(make_user):
    user = make_user("positional")
    assert user.persist_units() == []
    assert user.restore_units() == []


def test_restore_units_returns_empty_for_a_never_saved_unit(make_user):
    user = make_user("positional")
    result = user.restore_units(user.screen_module.plain)
    assert result == []


def test_restore_units_returns_empty_for_an_unreachable_unit(make_user):
    user = make_user("positional")
    stray = Edit("Stray", "nowhere")
    assert user.restore_units(stray) == []


def test_persist_units_saves_a_whole_block_including_nested_children(make_user):
    user = make_user("positional")
    mod = user.screen_module
    mod.plain.value = "leaf one"
    mod.inner.value = "leaf two"

    written = user.persist_units(mod.plain_block)
    assert written == [mod.plain_block]

    ns, path = user.persist_location(mod.plain_block)
    saved = user.get_objects(ns, path, "")
    children = {c["name"]: c["value"] for c in saved[""]["value"]}
    assert children == {"Plain": "leaf one", "Inner": "leaf two"}


def test_restore_units_restores_a_whole_block_including_nested_children(make_user):
    user = make_user("positional")
    mod = user.screen_module
    mod.plain.value = "leaf one"
    mod.inner.value = "leaf two"
    user.persist_units(mod.plain_block)

    object.__setattr__(mod.plain, "value", "tampered")
    object.__setattr__(mod.inner, "value", "tampered too")

    restored = user.restore_units(mod.plain_block)

    assert restored == [mod.plain_block]
    assert mod.plain.value == "leaf one"
    assert mod.inner.value == "leaf two"


def test_persist_units_on_a_shared_block_unit_uses_the_shared_namespace(make_user):
    user = make_user("shared_host")
    shared_mod = user.modules["blocks.shared"]

    written = user.persist_units(shared_mod.manual_edit)

    assert written == [shared_mod.manual_edit]
    ns, path = user.persist_location(shared_mod.manual_edit)
    assert ns == "@blocks.shared"


def test_persist_units_and_restore_units_on_a_shared_unit_work_from_a_different_screen(
    make_user,
):
    user = make_user("shared_host")
    shared_mod = user.modules["blocks.shared"]
    shared_mod.manual_edit.value = "set from SharedHost"
    user.persist_units(shared_mod.manual_edit)

    # switch to an unrelated screen -- identity/persistence for a shared unit
    # is anchored to its own module, not to whichever screen is currently active
    other = user.ensure_screen("positional")
    user.screen_module = other

    object.__setattr__(shared_mod.manual_edit, "value", "tampered")
    restored = user.restore_units(shared_mod.manual_edit)

    assert restored == [shared_mod.manual_edit]
    assert shared_mod.manual_edit.value == "set from SharedHost"


def test_persist_units_returns_only_units_it_actually_processed(make_user):
    # a mixed batch: one already-saved-and-unchanged, one genuinely new,
    # one unreachable -- the return value should reflect exactly that.
    user = make_user("positional")
    mod = user.screen_module
    user.persist_units(mod.plain)  # pre-save so it's unchanged this round
    stray = Edit("Stray", "nowhere")

    written = user.persist_units(mod.plain, mod.inner, stray)

    assert written == [mod.inner]
