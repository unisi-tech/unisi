"""
Tests for the persist-timing fix: prepare_result(raw, persist=False) (used
by progress() ticks and a dialog's own close notice) must not trigger
sync_keyed_persist()/_save_persist_if_needed(), and nothing that changed
before such a call may be lost -- it must still be saved, exactly once, by
the real end-of-request response (see _pending_persist_units in
persist.py's _init_persist).
"""
import pytest


@pytest.fixture
def count_writes(monkeypatch):
    """Count and record real SQLite writes, by wrapping Persist.save_changed
    / Persist.save_keyed at the class level. Returns an object with
    .changed (list of the persist_data passed to save_changed, per call)
    and .keyed (list of (namespace, path, context_key, value) per call)."""
    from unisi import persist as persist_module

    class Counts:
        changed = []
        keyed = []

    orig_save_changed = persist_module.Persist.save_changed
    orig_save_keyed = persist_module.Persist.save_keyed

    def patched_save_changed(self, user, current_screen, persist_data):
        Counts.changed.append([dict(state) for _, state in persist_data])
        return orig_save_changed(self, user, current_screen, persist_data)

    def patched_save_keyed(self, namespace, path, context_key, value):
        Counts.keyed.append((namespace, path, context_key, dict(value)))
        return orig_save_keyed(self, namespace, path, context_key, value)

    monkeypatch.setattr(persist_module.Persist, "save_changed", patched_save_changed)
    monkeypatch.setattr(persist_module.Persist, "save_keyed", patched_save_keyed)
    return Counts


@pytest.mark.asyncio
async def test_progress_ticks_do_not_write_to_disk(make_user, wire_send, count_writes):
    user = make_user("timing")
    send = wire_send(user)

    from unisi.common import ReceivedMessage

    msg = ReceivedMessage({"block": "Root", "element": "Run task", "event": "changed", "value": None})
    result = await user.result4message(msg)  # runs status="step1"->progress->"step2"->progress->"step3"

    assert count_writes.changed == []  # nothing written while the handler (incl. both progress ticks) ran

    await send(result)  # the real, final response

    assert len(count_writes.changed) == 1  # written exactly once, right here


@pytest.mark.asyncio
async def test_progress_ticks_do_not_lose_the_final_value(make_user, wire_send, count_writes):
    user = make_user("timing")
    send = wire_send(user)
    from unisi.common import ReceivedMessage

    msg = ReceivedMessage({"block": "Root", "element": "Run task", "event": "changed", "value": None})
    result = await user.result4message(msg)
    await send(result)

    saved_fields = count_writes.changed[0]
    assert any(f.get("value") == "step3" for f in saved_fields)

    ns, path = user.persist_location(user.screen_module.status)
    assert user.get_objects(ns, path, "")[""]["value"] == "step3"


@pytest.mark.asyncio
async def test_progress_ticks_still_deliver_interim_updates_to_the_client(make_user, wire_send):
    # the fix must not break the unrelated half of prepare_result's job:
    # each progress tick should still carry whatever changed since the last
    # one, same as before.
    user = make_user("timing")
    send = wire_send(user)
    from unisi.common import ReceivedMessage

    msg = ReceivedMessage({"block": "Root", "element": "Run task", "event": "changed", "value": None})
    result = await user.result4message(msg)
    await send(result)

    assert len(send.sent) == 3  # two progress ticks + the final response
    first_tick_units = [u["data"] for u in send.sent[0].updates]
    second_tick_units = [u["data"] for u in send.sent[1].updates]
    assert user.screen_module.status in first_tick_units
    assert user.screen_module.status in second_tick_units


@pytest.mark.asyncio
async def test_dialog_close_notice_does_not_write_before_the_callback_runs(
    make_user, wire_send, count_writes
):
    user = make_user("timing")
    send = wire_send(user)
    from unisi.common import ReceivedMessage

    open_msg = ReceivedMessage(
        {"block": "Root", "element": "Open dialog", "event": "changed", "value": None}
    )
    result = await user.result4message(open_msg)
    await send(result)
    assert user.active_dialog is not None
    count_writes.changed.clear()

    ok_msg = ReceivedMessage({"block": "Confirm?", "element": "Ok", "event": "changed", "value": None})
    result2 = await user.result4message(ok_msg)  # sends the close notice, THEN runs dialog_callback

    assert count_writes.changed == []  # close notice alone must not have saved anything
    assert user.screen_module.status.value == "dialog:Ok"  # but the callback DID run

    await send(result2)  # the real response

    assert len(count_writes.changed) == 1
    assert any(f.get("value") == "dialog:Ok" for f in count_writes.changed[0])
    assert user.active_dialog is None


@pytest.mark.asyncio
async def test_keyed_persist_is_also_deferred_and_not_lost(make_user, wire_send, count_writes):
    user = make_user("keyed")
    send = wire_send(user)
    from unisi.common import ReceivedMessage

    # establish the key first (see test_keyed_persist.py's note on first-ever
    # key evaluation), outside of anything being measured here
    async def _deliver(block, element, value):
        m = ReceivedMessage({"block": block, "element": element, "event": "changed", "value": value})
        r = await user.result4message(m)
        await send(r)

    await _deliver("Root", "Selector", "A")
    count_writes.keyed.clear()

    # a progress() (persist=False internally) in between two edits under the
    # SAME key must not write early, and must not cause the edit to be lost
    msg1 = ReceivedMessage(
        {"block": "Root", "element": "Single key field", "event": "changed", "value": "first"}
    )
    await user.result4message(msg1)
    await user.progress("halfway")  # persist=False internally
    assert count_writes.keyed == []

    await send(None)  # real end of THIS request

    assert len(count_writes.keyed) == 1
    assert count_writes.keyed[0][2] == "A"
    assert count_writes.keyed[0][3]["value"] == "first"


@pytest.mark.asyncio
async def test_multiple_progress_calls_before_one_real_change_accumulate_correctly(
    make_user, wire_send, count_writes
):
    # a handler that calls progress() several times with edits interleaved --
    # nothing from any earlier tick should be dropped from the final save.
    user = make_user("positional")
    send = wire_send(user)
    mod = user.screen_module

    from unisi.common import ReceivedMessage

    user.last_message = ReceivedMessage(
        {"block": "Root", "element": "trigger", "event": "changed", "value": None}
    )

    mod.flagged.value = "a"
    await user.progress("1")
    mod.flagged.value = "b"
    await user.progress("2")
    mod.flagged.value = "c"
    await user.progress("3")
    mod.flagged.value = "final"

    assert count_writes.changed == []

    await send(None)

    assert len(count_writes.changed) == 1
    assert any(f.get("value") == "final" for f in count_writes.changed[0])


@pytest.mark.asyncio
async def test_persist_disabled_users_are_unaffected_by_progress_persist_param(make_user):
    # session == 'autotest' disables persistence entirely (_persist_enabled);
    # progress()'s persist=False must not, say, blow up trying to reach a
    # nonexistent db in that mode.
    from unisi.users import User

    user = User("autotest")
    module = user.ensure_screen("timing")
    user.screen_module = module

    async def send(res, persist=True):
        return user.prepare_result(res, persist=persist)

    user.send = send
    await user.progress("no-op in autotest mode")  # must not raise
