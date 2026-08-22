"""
Shared pytest fixtures for the multimon.py unit tests.

multimon.py is architecturally distinct from the "pure logic" modules
covered under tests/units/ (units.py/tables.py/graphs.py/containers.py):
it's a multiprocessing-based IPC/monitoring subsystem with real module-
level side effects gated by config.froze_time/config.profile at import
time (spawning a background daemon process, or not, depending on their
values *at the moment unisi.multimon is first imported*). That earns it
its own directory and conftest.py, the same way tests/db_units/ and
tests/llmrag/ are split out from tests/units/ for their own distinct
subsystems.

Two things make this module's tests more delicate than most:

  * `from config import froze_time, monitor_tick, profile, pool` at the
    top of multimon.py COPIES those four values into multimon's own
    module namespace at import time -- they are not live references back
    to `config`. Mutating `config.pool` afterwards does nothing to
    `_get_pool()`; tests have to monkeypatch `multimon.pool` (or whichever
    of the four) directly, or -- to re-exercise the *module-level* gate
    itself (`if froze_time or profile: ... else: notify_monitor = None`)
    -- monkeypatch `config`'s attributes and importlib.reload(multimon)
    so the `from config import ...` line reruns.
  * `sys.modules['config']` is a single, process-global module shared
    across every test directory in the same pytest session (see
    unisi/utils.py's import-or-bootstrap logic at the top of the file).
    Whichever test directory happens to run first in a given session
    decides what `config` initially contains. Fixtures here never rely on
    a *particular* starting value -- only ever on the value they
    themselves just set, and always leave multimon back in its disabled
    (both falsy) state so a real fixtures_app-backed suite running later
    in the same session doesn't inherit a live background process.
"""
import importlib
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).parent
UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))

import unisi.utils  # noqa: E402  -- bootstraps sys.modules['config'] if nothing has yet
import config  # noqa: E402
import unisi.multimon as multimon  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_multimon_globals():
    """Safety-net snapshot/restore of multimon's own import-time-copied
    globals around every test in this file. Tests that actually reload
    the module (see reload_monitor below) do their own, more thorough
    teardown; this just catches any test that monkeypatches e.g.
    multimon.profile directly without going through that fixture.
    """
    snapshot = {
        k: getattr(multimon, k)
        for k in ('froze_time', 'monitor_tick', 'profile', 'pool', '_pool_instance')
    }

    yield

    for k, v in snapshot.items():
        setattr(multimon, k, v)


@pytest.fixture
def small_pool():
    """A real multiprocessing.Pool with a single worker, installed as
    multimon's lazy singleton for the duration of the test -- real
    subprocesses, just as few of them as possible to keep this fast.
    Terminated and unset on teardown (the snapshot/restore above only
    restores the *reference*, not a live process).
    """
    multimon.pool = 1
    multimon._pool_instance = None

    yield

    if multimon._pool_instance is not None:
        multimon._pool_instance.terminate()
        multimon._pool_instance.join()
        multimon._pool_instance = None


@pytest.fixture
def reload_monitor(monkeypatch):
    """Factory fixture: reload_monitor(froze_time=None, profile=0) ->
    the freshly-reloaded multimon module, with config.froze_time /
    config.profile set to the given values *before* the reload so its
    module-level `if froze_time or profile:` gate genuinely reruns (the
    real production code path, not a hand-simulated one).

    Always terminates any daemon process it spawned and reloads back to
    a disabled (both falsy) state on teardown -- this is the only fixture
    in the file that touches the real background process, which is why
    it's opt-in rather than autouse.
    """
    spawned = []

    def _reload(froze_time=None, profile=0):
        monkeypatch.setattr(config, 'froze_time', froze_time)
        monkeypatch.setattr(config, 'profile', profile)
        importlib.reload(multimon)
        proc = getattr(multimon, '_monitor_proc', None)
        if proc is not None:
            spawned.append(proc)
        return multimon

    yield _reload

    for proc in spawned:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
    monkeypatch.setattr(config, 'froze_time', None)
    monkeypatch.setattr(config, 'profile', 0)
    importlib.reload(multimon)
