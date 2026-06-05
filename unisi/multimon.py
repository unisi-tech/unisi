# Copyright © 2024 UNISI Tech. All rights reserved.
from __future__ import annotations  # defer annotation evaluation — required for
                                     # multiprocessing.Pool | None on Python 3.10+
import multiprocessing, time, asyncio, logging
from queue import Empty
from .utils import start_logging
from config import froze_time, monitor_tick, profile, pool

# ── Shared-array helpers ──────────────────────────────────────────────────────

_SHARED_ARRAY_SIZE = 512  # increased from 200 to handle longer session/event names


def _write_to_shared(shared_array: multiprocessing.Array, text: str) -> None:
    """Encode *text* and write it into *shared_array*.

    Uses the native ``Array.value`` setter which automatically appends a NUL
    terminator, making any stale bytes from a previously longer message
    invisible to the reader.  Raises ``ValueError`` when the payload is too
    long (the array needs one extra byte for the NUL).
    """
    data = text.encode()
    if len(data) >= len(shared_array):      # need at least one byte for NUL
        raise ValueError(
            f"Encoded message ({len(data)} B) exceeds shared array capacity "
            f"({len(shared_array) - 1} B usable)"
        )
    shared_array.value = data               # .value writes data + NUL terminator


def _read_from_shared(shared_array: multiprocessing.Array) -> str:
    """Read a NUL-terminated string from *shared_array*.

    ``Array.value`` returns bytes up to (but not including) the first ``\\x00``,
    matching exactly what ``_write_to_shared`` wrote.
    """
    return shared_array.value.decode()


# ── Process pool (lazy singleton) ────────────────────────────────────────────

_pool_instance: multiprocessing.Pool | None = None


def _get_pool() -> multiprocessing.Pool:
    """Return the shared process pool, creating it on first call."""
    global _pool_instance
    if _pool_instance is None:
        _pool_instance = multiprocessing.Pool(pool)
    return _pool_instance


# ── External-process runner ───────────────────────────────────────────────────

async def run_external_process(long_running_task, *args, progress_callback=None, **kwargs):
    """Run *long_running_task* in a worker process without blocking the event loop.

    Progress reporting contract
    ---------------------------
    If *progress_callback* is provided the last positional argument must be
    either ``None`` (a fresh ``multiprocessing.Queue`` is injected automatically)
    or an existing queue the task writes messages to.  The task signals
    completion by putting a ``None`` sentinel into the queue.
    """
    queue = None
    if progress_callback is not None:
        if not args:
            raise ValueError(
                "progress_callback requires at least one positional arg; "
                "pass None as the last arg to let run_external_process create the queue."
            )
        if args[-1] is None:
            queue = multiprocessing.Manager().Queue()
            args = (*args[:-1], queue)
        else:
            queue = args[-1]

    result = _get_pool().apply_async(long_running_task, args, kwargs)

    if progress_callback is not None and queue is not None:
        # Use get_nowait() + Empty catch so the event loop is never blocked.
        # run_in_executor(queue.get) is an alternative but occupies a thread-pool
        # slot indefinitely and has no timeout, making it harder to reason about.
        while not result.ready() or not queue.empty():
            try:
                message = queue.get_nowait()
                if message is None:
                    break
                await asyncio.gather(
                    progress_callback(message),
                    asyncio.sleep(monitor_tick),
                )
            except Empty:
                await asyncio.sleep(monitor_tick)

    # After the progress loop the worker may still be serialising its return
    # value over IPC (it put the None sentinel just before returning).
    # Poll asynchronously until ready() is True so result.get() never blocks.
    while not result.ready():
        await asyncio.sleep(monitor_tick)

    return result.get()


# ── Monitor process ───────────────────────────────────────────────────────────

_logging_lock = multiprocessing.Lock()
logging_lock = _logging_lock   # public alias — imported by users.py
_SPLITTER = "~"

# Protocol status codes — kept in a class so dotted names (e.g. _S.ENTER) can
# be used directly in `match/case` as value patterns.  Bare module-level names
# in `case` clauses are treated as capture patterns by Python's parser, which
# makes every subsequent branch unreachable (SyntaxError in 3.10+).
class _S:
    ENTER         = "+"   # session entered the async handler queue
    EXIT_HANDLER  = "-"   # session handler finished
    EXTERNAL_DONE = "e"   # external process completed
    EXTERNAL_CALL = "p"   # external process was called (no freeze alarm)


def _monitor_process(shared_arr: multiprocessing.Array) -> None:
    """Background monitoring process — never returns (runs as daemon).

    Tracks per-session events and emits log warnings when:
    * any session has been waiting longer than *froze_time* seconds, OR
    * a handler took longer than *profile* seconds.

    The freeze check is done by comparing wall-clock timestamps for *each*
    active session independently, so a new event on session B cannot hide a
    hang on session A.
    """
    # session_name -> [event_name, start_timestamp, track_freeze]
    # track_freeze=True  : session should trigger the freeze alarm if it lingers
    # track_freeze=False : session is waiting on an external process — no alarm
    session_status: dict[str, list] = {}
    last_freeze_check = time.time()

    start_logging()

    while True:
        # ── Wait for a message ────────────────────────────────────────────────
        while shared_arr[0] == b"\x00":
            time.sleep(monitor_tick)

            if froze_time:
                now = time.time()
                if now - last_freeze_check >= monitor_tick:
                    last_freeze_check = now
                    _check_for_frozen_sessions(session_status, now)

        # ── Decode, release slot, dispatch ───────────────────────────────────
        raw = _read_from_shared(shared_arr)
        shared_arr[0] = b"\x00"          # free as early as possible

        parts = raw.split(_SPLITTER)
        if len(parts) < 3:
            with _logging_lock:
                logging.warning(f"Monitor: malformed message {parts!r} — skipped")
            continue

        code, sname, event = parts[0], parts[1], parts[2]

        match code:
            case _S.ENTER | _S.EXTERNAL_DONE:
                # Session is now waiting for a handler / external call returned.
                # Arm the freeze alarm (track_freeze=True).
                session_status[sname] = [event, time.time(), True]

            case _S.EXIT_HANDLER:
                entry = session_status.pop(sname, None)
                if entry is not None:
                    event_name, tstart, _ = entry   # ignore track_freeze flag
                    duration = time.time() - tstart
                    if profile and duration > profile:
                        with _logging_lock:
                            logging.warning(
                                f"Event handler '{event_name}' for session '{sname}' "
                                f"took {duration:.3f} s (threshold: {profile} s)"
                            )

            case _S.EXTERNAL_CALL:
                # Session is occupied by an external process — record for
                # profiling, but do NOT arm the freeze alarm (track_freeze=False).
                session_status[sname] = [event, time.time(), False]

            case _:
                with _logging_lock:
                    logging.warning(
                        f"Monitor: unknown status code '{code}' from session '{sname}'"
                    )


def _check_for_frozen_sessions(
    session_status: dict[str, list], now: float
) -> None:
    """Emit a warning for every *freeze-tracked* session that has waited too long.

    Sessions registered with ``track_freeze=False`` (i.e. those waiting on an
    external process via ``_STATUS_EXTERNAL_CALL``) are intentionally excluded
    from the alarm — their long runtime is expected.

    Resets the start timestamp of each offending session so the same hang is
    reported at most once per *froze_time* interval rather than every tick.
    """
    frozen = [
        (name, info[0], info[1])
        for name, info in session_status.items()
        if info[2] and (now - info[1] > froze_time)   # info[2] = track_freeze flag
    ]
    if not frozen:
        return

    frozen.sort(key=lambda x: x[2])          # oldest first
    lines = "\n".join(
        f"  session={name!r}  event={event!r}  waiting={now - tstart:.1f} s"
        for name, event, tstart in frozen
    )
    with _logging_lock:
        logging.warning("Freeze detected! Hung sessions:\n" + lines)

    # Back-fill timestamps to avoid log spam on the next tick
    for name, _, _ in frozen:
        session_status[name][1] = now


# ── Module-level initialisation ───────────────────────────────────────────────

if froze_time or profile:
    _monitor_shared_arr = multiprocessing.Array("c", _SHARED_ARRAY_SIZE)
    # multiprocessing.Array is zero-initialised, so no explicit init needed.

    _notify_lock: asyncio.Lock | None = None

    async def notify_monitor(status: str, session: str, event: str) -> None:
        """Send a status update to the monitor process.

        An asyncio.Lock serialises concurrent callers so two coroutines can
        never overwrite each other's message in the shared slot.
        The lock is created lazily inside the running event loop.
        """
        global _notify_lock
        if _notify_lock is None:
            _notify_lock = asyncio.Lock()

        message = f"{status}{_SPLITTER}{session}{_SPLITTER}{event}"

        async with _notify_lock:
            while _monitor_shared_arr[0] != b"\x00":
                await asyncio.sleep(monitor_tick)
            _write_to_shared(_monitor_shared_arr, message)

    _monitor_proc = multiprocessing.Process(
        target=_monitor_process,
        args=(_monitor_shared_arr,),
        daemon=True,    # automatically terminated when the main process exits
    )
    _monitor_proc.start()

else:
    notify_monitor = None