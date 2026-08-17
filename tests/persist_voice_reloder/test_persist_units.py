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


# --- context_key: named, independent on-demand save/restore slots ---------
# persist_units/restore_units default to the same (namespace, path,
# context_key='') row positional persist=True would use (see the tests
# above). Passing context_key writes/reads a different, explicitly-named
# row instead, so a unit can hold several independent on-demand snapshots
# side by side without disturbing the default '' slot or each other.

def test_persist_units_with_context_key_writes_to_a_separate_slot_from_the_default(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain
    plain.value = "draft value"

    written = user.persist_units(plain, context_key="draft")

    assert written == [plain]
    ns, path = user.persist_location(plain)
    assert user.get_objects(ns, path, "") == {}  # default '' slot: untouched
    assert user.get_objects(ns, path, "draft")["draft"]["value"] == "draft value"


def test_persist_units_context_key_none_behaves_like_the_default_empty_string(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain
    plain.value = "same slot either way"

    first = user.persist_units(plain)                     # context_key=None
    second = user.persist_units(plain, context_key="")     # explicit '' -> same row

    assert first == [plain]
    assert second == []  # already identical to what the first call just wrote


def test_restore_units_with_context_key_loads_the_matching_named_snapshot(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain

    plain.value = "value A"
    user.persist_units(plain, context_key="v1")
    plain.value = "value B"
    user.persist_units(plain, context_key="v2")

    object.__setattr__(plain, "value", "tampered")
    restored_v1 = user.restore_units(plain, context_key="v1")
    assert restored_v1 == [plain]
    assert plain.value == "value A"

    object.__setattr__(plain, "value", "tampered again")
    restored_v2 = user.restore_units(plain, context_key="v2")
    assert restored_v2 == [plain]
    assert plain.value == "value B"


def test_restore_units_with_context_key_skips_silently_for_an_unsaved_context(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain
    user.persist_units(plain)  # only the default '' slot has data

    result = user.restore_units(plain, context_key="never-saved")

    assert result == []
    assert plain.value == "unflagged"  # untouched -- nothing was applied


def test_persist_units_with_context_key_skips_a_write_when_unchanged(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain

    first = user.persist_units(plain, context_key="checkpoint")
    second = user.persist_units(plain, context_key="checkpoint")  # nothing changed since

    assert first == [plain]
    assert second == []


def test_persist_units_with_context_key_writes_again_after_a_real_change(make_user):
    user = make_user("positional")
    plain = user.screen_module.plain

    user.persist_units(plain, context_key="checkpoint")
    plain.value = "changed under checkpoint"
    written = user.persist_units(plain, context_key="checkpoint")

    assert written == [plain]


def test_persist_units_with_context_key_is_visible_via_get_contexts(make_user):
    # ties persist_units' context_key into the same general search API
    # (§13.4) that keyed-persist context_keys are enumerated through.
    user = make_user("positional")
    plain = user.screen_module.plain

    plain.value = "a"
    user.persist_units(plain, context_key="alpha")
    plain.value = "b"
    user.persist_units(plain, context_key="beta")

    ns, path = user.persist_location(plain)
    assert set(user.get_contexts(ns, path, "..")) == {"alpha", "beta"}


def test_persist_units_with_context_key_returns_only_units_it_actually_wrote(make_user):
    user = make_user("positional")
    mod = user.screen_module
    user.persist_units(mod.plain, context_key="checkpoint")  # pre-save, unchanged this round
    stray = Edit("Stray", "nowhere")

    written = user.persist_units(mod.plain, mod.inner, stray, context_key="checkpoint")

    assert written == [mod.inner]


# --- Block/ParamBlock recursion: whole subtree, however deep --------------
# __getstate__/_json_ready (save) and _smart_apply_dict/_rebuild_value
# (restore) already walk a Block's `value` recursively, so passing a Block
# to persist_units/restore_units covers every nested unit inside it, not
# just its own top-level fields -- see the "...saves_a_whole_block..." tests
# above for one level of nesting. These confirm it holds for a *grandchild*
# two levels down too, and that it composes correctly with context_key.

def test_persist_units_saves_a_deeply_nested_block_two_levels_down(make_user):
    user = make_user("positional")
    mod = user.screen_module
    mod.leaf_a.value = "deep one"
    mod.leaf_b.value = "deep two"

    written = user.persist_units(mod.outer_nested_block)
    assert written == [mod.outer_nested_block]

    ns, path = user.persist_location(mod.outer_nested_block)
    saved = user.get_objects(ns, path, "")[""]
    inner = saved["value"][0]  # "Inner nested block", one level below "Outer nested block"
    grandchildren = {c["name"]: c["value"] for c in inner["value"]}
    assert grandchildren == {"Leaf A": "deep one", "Leaf B": "deep two"}


def test_restore_units_restores_a_deeply_nested_block_two_levels_down(make_user):
    user = make_user("positional")
    mod = user.screen_module
    mod.leaf_a.value = "deep one"
    mod.leaf_b.value = "deep two"
    user.persist_units(mod.outer_nested_block)

    object.__setattr__(mod.leaf_a, "value", "tampered")
    object.__setattr__(mod.leaf_b, "value", "tampered")

    restored = user.restore_units(mod.outer_nested_block)

    assert restored == [mod.outer_nested_block]
    assert mod.leaf_a.value == "deep one"
    assert mod.leaf_b.value == "deep two"


def test_persist_units_with_context_key_saves_whole_block_subtree_under_a_named_slot(make_user):
    user = make_user("positional")
    mod = user.screen_module
    mod.plain.value = "ctx leaf one"
    mod.inner.value = "ctx leaf two"

    written = user.persist_units(mod.plain_block, context_key="checkpoint")
    assert written == [mod.plain_block]

    ns, path = user.persist_location(mod.plain_block)
    assert user.get_objects(ns, path, "") == {}  # default '' slot: untouched
    saved = user.get_objects(ns, path, "checkpoint")["checkpoint"]
    children = {c["name"]: c["value"] for c in saved["value"]}
    assert children == {"Plain": "ctx leaf one", "Inner": "ctx leaf two"}


def test_persist_units_with_context_key_skips_a_write_when_block_unchanged(make_user):
    user = make_user("positional")
    plain_block = user.screen_module.plain_block

    first = user.persist_units(plain_block, context_key="checkpoint")
    second = user.persist_units(plain_block, context_key="checkpoint")

    assert first == [plain_block]
    assert second == []


def test_persist_units_and_restore_units_multiple_named_block_snapshots_round_trip(make_user):
    # the combined case: several independent whole-subtree snapshots of the
    # SAME block, each restorable on its own, none of them touching the
    # default '' slot or each other -- grandchildren included both ways.
    user = make_user("positional")
    mod = user.screen_module

    mod.leaf_a.value = "A-alpha"
    mod.leaf_b.value = "B-alpha"
    user.persist_units(mod.outer_nested_block, context_key="alpha")

    mod.leaf_a.value = "A-beta"
    mod.leaf_b.value = "B-beta"
    user.persist_units(mod.outer_nested_block, context_key="beta")

    ns, path = user.persist_location(mod.outer_nested_block)
    assert user.get_objects(ns, path, "") == {}  # default slot was never written

    object.__setattr__(mod.leaf_a, "value", "tampered")
    object.__setattr__(mod.leaf_b, "value", "tampered")
    restored_alpha = user.restore_units(mod.outer_nested_block, context_key="alpha")
    assert restored_alpha == [mod.outer_nested_block]
    assert mod.leaf_a.value == "A-alpha"
    assert mod.leaf_b.value == "B-alpha"

    object.__setattr__(mod.leaf_a, "value", "tampered again")
    object.__setattr__(mod.leaf_b, "value", "tampered again")
    restored_beta = user.restore_units(mod.outer_nested_block, context_key="beta")
    assert restored_beta == [mod.outer_nested_block]
    assert mod.leaf_a.value == "A-beta"
    assert mod.leaf_b.value == "B-beta"
