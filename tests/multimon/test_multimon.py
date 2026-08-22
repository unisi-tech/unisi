# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/multimon.py: the shared-array mailbox helpers, the
lazy process pool, run_external_process(), the monitor's message parsing
and per-session freeze/profile bookkeeping, and the config-gated module
import itself (notify_monitor is None vs. a real background process).

Layout
──────
TestSharedArrayHelpers   -- _write_to_shared / _read_from_shared: boundary
                            size, round trip, stale-byte truncation.
TestGetPool               -- _get_pool()'s lazy-singleton behaviour.
TestRunExternalProcess    -- the async orchestration: plain calls, the
                            progress_callback contract (auto-created vs.
                            caller-supplied queue), error propagation.
TestParseMessage          -- _parse_message(): the wire-format split.
TestDispatchMessage       -- _dispatch_message(): the per-status-code
                            session_status bookkeeping (this -- along with
                            TestParseMessage -- is what _monitor_process's
                            inner loop delegates to; see module docstring
                            in multimon.py for why they were extracted).
TestCheckForFrozenSessions -- the freeze-alarm sweep.
TestModuleImportGate      -- config.froze_time/profile -> notify_monitor
                            is None vs. a real callable + a live daemon
                            process, exercised through an actual
                            importlib.reload (see conftest.py).

Regression tests are labelled `test_regression_*` (matching
tests/units/test_units.py's and tests/db_units/test_db.py's convention):

  * _parse_message (formerly inlined in _monitor_process) used to keep
    only `parts[2]` as `event`, silently dropping everything after a
    *second* '~' in the raw message. `event` is typically
    str(some_received_message), which embeds arbitrary user-typed text --
    if that text itself contains '~' (a plausible thing to type into any
    text field), the tail of the event was lost. Fixed by rejoining
    parts[2:] -- see TestParseMessage.
"""
import asyncio
import logging
import time
from queue import Empty

import pytest

from unisi.multimon import (
    _write_to_shared, _read_from_shared, _SHARED_ARRAY_SIZE,
    _get_pool, run_external_process,
    _parse_message, _dispatch_message, _check_for_frozen_sessions,
    _S,
)
import multiprocessing


# Module-level (and therefore picklable) targets for the real-Pool tests in
# TestRunExternalProcess -- multiprocessing.Pool.apply_async needs to look
# these up by "module.qualname" in the worker process, so they can't be
# closures or lambdas.

def _double(x):
    return x * 2


def _raise_runtime_error():
    raise RuntimeError("boom from worker")


def _task_with_progress(n_steps, queue):
    for i in range(n_steps):
        queue.put(f"step-{i}")
    queue.put(None)  # sentinel
    return f"done-{n_steps}"


# ──────────────────────────────────────────────────────────────────────── #
#  Shared-array helpers                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestSharedArrayHelpers:
    def make_array(self):
        return multiprocessing.Array('c', _SHARED_ARRAY_SIZE)

    def test_round_trip(self):
        arr = self.make_array()
        _write_to_shared(arr, "hello world")
        assert _read_from_shared(arr) == "hello world"

    def test_empty_string_round_trip(self):
        arr = self.make_array()
        _write_to_shared(arr, "")
        assert _read_from_shared(arr) == ""

    def test_payload_of_exactly_capacity_minus_one_succeeds(self):
        arr = self.make_array()
        payload = "x" * (len(arr) - 1)
        _write_to_shared(arr, payload)
        assert _read_from_shared(arr) == payload

    def test_payload_of_exactly_capacity_raises(self):
        arr = self.make_array()
        payload = "x" * len(arr)
        with pytest.raises(ValueError, match="exceeds shared array capacity"):
            _write_to_shared(arr, payload)

    def test_payload_over_capacity_raises(self):
        arr = self.make_array()
        with pytest.raises(ValueError):
            _write_to_shared(arr, "x" * (len(arr) + 50))

    def test_shorter_message_after_longer_one_has_no_stale_tail(self):
        """Array.value's NUL terminator makes any bytes left over from a
        previous, longer write invisible to the next read -- this is the
        whole reason _SHARED_ARRAY_SIZE can safely be reused as a
        single-slot mailbox across many differently-sized messages."""
        arr = self.make_array()
        _write_to_shared(arr, "A" * 50)
        assert _read_from_shared(arr) == "A" * 50
        _write_to_shared(arr, "BB")
        assert _read_from_shared(arr) == "BB"

    def test_non_ascii_payload_round_trips(self):
        arr = self.make_array()
        _write_to_shared(arr, "session_привет_日本語")
        assert _read_from_shared(arr) == "session_привет_日本語"


# ──────────────────────────────────────────────────────────────────────── #
#  _get_pool                                                               #
# ──────────────────────────────────────────────────────────────────────── #

class TestGetPool:
    def test_returns_the_same_instance_on_repeated_calls(self, small_pool):
        first = _get_pool()
        second = _get_pool()
        assert first is second

    def test_pool_actually_dispatches_work(self, small_pool):
        pool = _get_pool()
        assert pool.apply(_double, (21,)) == 42


# ──────────────────────────────────────────────────────────────────────── #
#  run_external_process                                                    #
# ──────────────────────────────────────────────────────────────────────── #

class TestRunExternalProcess:
    @pytest.mark.asyncio
    async def test_plain_call_returns_the_worker_result(self, small_pool):
        result = await run_external_process(_double, 21)
        assert result == 42

    @pytest.mark.asyncio
    async def test_worker_exception_propagates_to_the_caller(self, small_pool):
        with pytest.raises(RuntimeError, match="boom from worker"):
            await run_external_process(_raise_runtime_error)

    @pytest.mark.asyncio
    async def test_progress_callback_with_no_positional_args_raises(self, small_pool):
        async def cb(message):
            pass

        with pytest.raises(ValueError, match="at least one positional arg"):
            await run_external_process(_double, progress_callback=cb)

    @pytest.mark.asyncio
    async def test_progress_callback_auto_creates_the_queue(self, small_pool):
        """Last positional arg is None -> run_external_process injects a
        fresh Manager().Queue() in its place."""
        received = []

        async def cb(message):
            received.append(message)

        result = await run_external_process(_task_with_progress, 3, None, progress_callback=cb)

        assert result == "done-3"
        assert received == ["step-0", "step-1", "step-2"]

    @pytest.mark.asyncio
    async def test_progress_callback_uses_a_caller_supplied_queue(self, small_pool):
        """Last positional arg is an existing queue (not None) -> that
        exact queue is used rather than a fresh one being created."""
        manager = multiprocessing.Manager()
        queue = manager.Queue()
        received = []

        async def cb(message):
            received.append(message)

        result = await run_external_process(_task_with_progress, 2, queue, progress_callback=cb)

        assert result == "done-2"
        assert received == ["step-0", "step-1"]
        assert queue.empty()  # the sentinel was consumed, nothing left behind

    @pytest.mark.asyncio
    async def test_progress_callback_receives_messages_in_order(self, small_pool):
        received = []

        async def cb(message):
            received.append(message)
            await asyncio.sleep(0)  # make sure ordering survives a real await

        await run_external_process(_task_with_progress, 5, None, progress_callback=cb)
        assert received == [f"step-{i}" for i in range(5)]


# ──────────────────────────────────────────────────────────────────────── #
#  _parse_message                                                          #
# ──────────────────────────────────────────────────────────────────────── #

class TestParseMessage:
    def test_well_formed_message(self):
        assert _parse_message("+~session1~root/None->changed(42)") == (
            "+", "session1", "root/None->changed(42)"
        )

    def test_fewer_than_three_parts_returns_none(self):
        assert _parse_message("+~session1") is None
        assert _parse_message("nosplitter") is None

    def test_empty_event_is_valid(self):
        assert _parse_message("e~session1~") == ("e", "session1", "")

    def test_regression_extra_splitter_in_event_is_preserved_not_truncated(self):
        """event commonly comes from str(some_received_message), which
        embeds arbitrary user-typed text. If that text itself contains
        '~', the raw message has more than 3 '~'-separated parts -- the
        old inline parsing kept only parts[2], silently losing everything
        after the second '~'."""
        raw = "-~session1~root/field->changed(a~b~c)"
        assert _parse_message(raw) == ("-", "session1", "root/field->changed(a~b~c)")

    def test_session_name_itself_is_not_rejoined(self):
        """Only content from the 3rd part onward is rejoined into event;
        parts[1] (session) is used as-is, matching the documented
        assumption that code/session are simple identifiers."""
        code, sname, event = _parse_message("+~sess~a~b~c")
        assert sname == "sess"
        assert event == "a~b~c"


# ──────────────────────────────────────────────────────────────────────── #
#  _dispatch_message                                                       #
# ──────────────────────────────────────────────────────────────────────── #

class TestDispatchMessage:
    def test_enter_arms_the_freeze_alarm(self):
        status = {}
        _dispatch_message(_S.ENTER, "s1", "ev1", status, now=100.0)
        assert status == {"s1": ["ev1", 100.0, True]}

    def test_external_done_behaves_like_enter(self):
        status = {}
        _dispatch_message(_S.EXTERNAL_DONE, "s1", "ev1", status, now=100.0)
        assert status == {"s1": ["ev1", 100.0, True]}

    def test_external_call_does_not_arm_the_freeze_alarm(self):
        status = {}
        _dispatch_message(_S.EXTERNAL_CALL, "s1", "ev1", status, now=100.0)
        assert status == {"s1": ["ev1", 100.0, False]}

    def test_exit_handler_removes_the_session(self):
        status = {"s1": ["ev1", 100.0, True]}
        _dispatch_message(_S.EXIT_HANDLER, "s1", "ignored", status, now=105.0)
        assert status == {}

    def test_exit_handler_for_unknown_session_does_not_raise(self):
        status = {}
        _dispatch_message(_S.EXIT_HANDLER, "never-entered", "ignored", status, now=1.0)
        assert status == {}

    def test_exit_handler_leaves_other_sessions_untouched(self):
        status = {"s1": ["ev1", 100.0, True], "s2": ["ev2", 50.0, False]}
        _dispatch_message(_S.EXIT_HANDLER, "s1", "ignored", status, now=105.0)
        assert status == {"s2": ["ev2", 50.0, False]}

    def test_unknown_status_code_logs_a_warning_and_does_not_raise(self, caplog):
        status = {}
        with caplog.at_level(logging.WARNING):
            _dispatch_message("?", "s1", "ev1", status, now=1.0)
        assert status == {}
        assert "unknown status code" in caplog.text

    def test_exit_handler_over_profile_threshold_logs_a_warning(self, caplog, monkeypatch):
        import unisi.multimon as multimon
        monkeypatch.setattr(multimon, "profile", 1.0)
        status = {"s1": ["my_event", 100.0, True]}
        with caplog.at_level(logging.WARNING):
            _dispatch_message(_S.EXIT_HANDLER, "s1", "ignored", status, now=102.0)
        assert "my_event" in caplog.text
        assert "s1" in caplog.text

    def test_exit_handler_under_profile_threshold_does_not_log(self, caplog, monkeypatch):
        import unisi.multimon as multimon
        monkeypatch.setattr(multimon, "profile", 5.0)
        status = {"s1": ["my_event", 100.0, True]}
        with caplog.at_level(logging.WARNING):
            _dispatch_message(_S.EXIT_HANDLER, "s1", "ignored", status, now=101.0)
        assert caplog.text == ""

    def test_exit_handler_with_profile_disabled_never_logs(self, caplog, monkeypatch):
        import unisi.multimon as multimon
        monkeypatch.setattr(multimon, "profile", 0)
        status = {"s1": ["my_event", 0.0, True]}
        with caplog.at_level(logging.WARNING):
            _dispatch_message(_S.EXIT_HANDLER, "s1", "ignored", status, now=10_000.0)
        assert caplog.text == ""

    def test_now_defaults_to_current_time_when_not_supplied(self):
        status = {}
        before = time.time()
        _dispatch_message(_S.ENTER, "s1", "ev1", status)
        after = time.time()
        assert before <= status["s1"][1] <= after


# ──────────────────────────────────────────────────────────────────────── #
#  _check_for_frozen_sessions                                              #
# ──────────────────────────────────────────────────────────────────────── #

class TestCheckForFrozenSessions:
    """_check_for_frozen_sessions reads `froze_time` as a module-level
    global (mirroring _dispatch_message's `profile`), not as a parameter
    -- in production it's only ever called from inside the `if
    froze_time:` guard in _monitor_process, so froze_time is guaranteed
    truthy there. These tests call it directly, so they set it themselves.
    All scenarios below assume froze_time=5.0 unless noted otherwise.
    """

    @pytest.fixture(autouse=True)
    def _froze_time(self, monkeypatch):
        import unisi.multimon as multimon
        monkeypatch.setattr(multimon, "froze_time", 5.0)

    def test_no_sessions_waiting_past_threshold_logs_nothing(self, caplog):
        status = {"s1": ["ev", 9.0, True]}
        with caplog.at_level(logging.WARNING):
            _check_for_frozen_sessions(status, now=10.0)  # only 1s elapsed
        assert caplog.text == ""

    def test_session_past_threshold_logs_a_warning(self, caplog):
        status = {"s1": ["ev", 0.0, True]}
        with caplog.at_level(logging.WARNING):
            _check_for_frozen_sessions(status, now=10.0)
        assert "Freeze detected" in caplog.text
        assert "s1" in caplog.text
        assert "ev" in caplog.text

    def test_track_freeze_false_sessions_are_excluded(self, caplog):
        """Sessions waiting on an external process (_S.EXTERNAL_CALL) are
        expected to run long -- they must never trip the freeze alarm."""
        status = {"s_ext": ["ev", 0.0, False]}
        with caplog.at_level(logging.WARNING):
            _check_for_frozen_sessions(status, now=1000.0)
        assert caplog.text == ""

    def test_offending_sessions_get_their_timestamp_back_filled(self):
        """So the same hang isn't re-reported on every single tick -- only
        once per elapsed froze_time interval."""
        status = {"s1": ["ev", 0.0, True]}
        _check_for_frozen_sessions(status, now=10.0)
        assert status["s1"][1] == 10.0

    def test_back_filled_session_does_not_immediately_refire(self, caplog):
        status = {"s1": ["ev", 0.0, True]}
        _check_for_frozen_sessions(status, now=10.0)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            _check_for_frozen_sessions(status, now=10.1)  # barely any time passed since back-fill
        assert caplog.text == ""

    def test_non_offending_sessions_are_not_back_filled(self):
        status = {"s_new": ["ev", 9.0, True], "s_old": ["ev", 0.0, True]}
        _check_for_frozen_sessions(status, now=10.0)
        assert status["s_new"][1] == 9.0   # untouched -- wasn't frozen
        assert status["s_old"][1] == 10.0  # back-filled -- was frozen

    def test_multiple_frozen_sessions_all_reported_and_back_filled(self):
        status = {
            "s1": ["ev1", 0.0, True],
            "s2": ["ev2", 1.0, True],
        }
        _check_for_frozen_sessions(status, now=10.0)
        assert status["s1"][1] == 10.0
        assert status["s2"][1] == 10.0


# ──────────────────────────────────────────────────────────────────────── #
#  Module import gate: notify_monitor is None vs. a live background process #
# ──────────────────────────────────────────────────────────────────────── #

class TestModuleImportGate:
    def test_disabled_by_default_leaves_notify_monitor_none(self, reload_monitor):
        m = reload_monitor(froze_time=None, profile=0)
        assert m.notify_monitor is None
        assert not hasattr(m, '_monitor_proc') or m._monitor_proc is None

    def test_froze_time_alone_enables_the_monitor(self, reload_monitor):
        m = reload_monitor(froze_time=5.0, profile=0)
        assert callable(m.notify_monitor)
        assert m._monitor_proc.is_alive()

    def test_profile_alone_enables_the_monitor(self, reload_monitor):
        """The gate is `if froze_time or profile:` -- either one alone is
        enough, not just froze_time."""
        m = reload_monitor(froze_time=None, profile=0.5)
        assert callable(m.notify_monitor)
        assert m._monitor_proc.is_alive()

    @pytest.mark.asyncio
    async def test_notify_monitor_message_is_actually_consumed_by_the_background_process(self, reload_monitor):
        """End-to-end smoke test with a *real* background process (not a
        simulated one): a message sent through notify_monitor should be
        picked off the shared array shortly after, proving the daemon
        process genuinely receives and processes it over real IPC."""
        m = reload_monitor(froze_time=5.0, profile=0)

        await m.notify_monitor('+', 'sess_x', 'ev_x')

        consumed = False
        for _ in range(300):  # generous bound; monitor_tick default is 5ms
            if m._monitor_shared_arr[0] == b'\x00':
                consumed = True
                break
            await asyncio.sleep(0.01)

        assert consumed
