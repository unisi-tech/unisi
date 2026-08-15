# Unit tests for persist.py / users.py

## Setup

```
pip install pytest pytest-asyncio
```

## Run

From the repo root:

```
pytest tests/unit/
```

## Layout

```
tests/unit/
    conftest.py          shared fixtures (make_user, wire_send, deliver)
    fixtures_app/         a small throwaway UNISI app the fixtures load screens from
        config.py
        screens/
            positional.py  persist=True unit + block, and unflagged units for contrast
            keyed.py        persist=<function>, single- and multi-value keys
            timing.py       progress() + Dialog, for the persist-timing tests
            shared_host.py  imports blocks/shared.py
        blocks/
            shared.py       a persist=True block and an unflagged one, both shared
    test_helpers.py           pure functions: _is_flag_persist, _encode_context_key,
                               _path_key, _split_template, _template_to_like, _escape_like
    test_simple_store.py      get_key / set_key / get_keys / remove_key / remove_keys
    test_positional_persist.py   persist=True: save, restore on reconnect, whole-block save
    test_keyed_persist.py     persist=<function>: key-change detection, active flag,
                               context_key format, the first-key-evaluation edge case
    test_object_search.py     get_objects / get_contexts (exact match and '..' templates)
    test_persist_units.py     User.persist_units / User.restore_units
    test_shared_blocks.py     blocks/ module identity, cascade, cross-screen stability
    test_persist_timing.py    progress()/dialog persist=False: no premature writes,
                               nothing lost, client-facing updates unaffected
    test_messaging.py         find_path, find_element, register_changed_unit's echo
                               suppression, prepare_result's raw-shape handling
```

83 fast tests (no server, no network -- each gets its own throwaway SQLite file,
removed at teardown) plus 7 that count real `Persist.save_changed`/`save_keyed`
calls with `monkeypatch` to prove writes happen exactly once, at the right moment.

## Design notes

- **Why not pure mocks everywhere.** persist.py and users.py aren't pure
  functions -- screen loading, tree position (`_parents`), and SQLite I/O are
  central to what they do. `test_helpers.py` unit-tests the handful of
  genuinely pure functions directly; everything else runs against a real
  `User` and a real (throwaway) SQLite file via `fixtures_app/`, because
  faking that layer would mostly end up testing the fakes instead of the code.

- **One fixture app, many sessions.** `unisi`'s module loader
  (`ModulesMixin`) keys `screens/`/`blocks/` modules into `sys.modules` by
  dotted name, so running several different fixture-app directories in one
  process risks cross-test collisions. There's one shared `fixtures_app/`
  for the whole run; isolation between tests comes from `make_user` handing
  out a fresh, never-reused session id (a fresh SQLite file) each call.

- **`make_user(screen, session=None)`.** Session defaults to a fresh id per
  call. Pass an explicit `session` to get a *second* `User` backed by the
  *same* SQLite file as an earlier one -- the way to simulate a reconnect
  and check that persisted state actually comes back (a single `User`
  instance restores once at construction time and won't show you that on
  its own).

- **`deliver(user, block, element, event, value)`** simulates one full
  client round trip -- `result4message` then the real `send(result)` -- the
  same shape as one iteration of `server.py`'s `websocket_handler` loop.
  `block` addresses a nested element the same way real client messages and
  `persist_location` both do: leaf-first, the element's own name excluded,
  immediate container first, root last (e.g. `"Plain block@Root"` for a
  unit inside `plain_block` inside `Root` -- see `find_element`/`find_path`).

- **`register_changed_unit` is a no-op with no active message.** Calling it
  (directly, or indirectly via `unit.value = x`) outside of `result4message`
  processing (i.e. `user.last_message` is `None`) does not add the unit to
  `changed_units` at all -- this is existing framework behavior, not
  something these tests work around. `test_messaging.py` covers it
  directly; other files that mutate a unit without going through `deliver`
  set `user.last_message` first where it matters.

## What isn't covered here

This exercises the persistence subsystem and the surrounding request
lifecycle thoroughly, but users.py has some corners this pass didn't reach:
voice command integration (`voicecom.py`'s use of `active_dialog`), the
cross-user `sync_dbupdates`/`Dbtable` live-update path, and hot-reload
(`reloader.py`'s `sync_send`). None of those are exercised by any test here
-- worth a follow-up pass if they matter to you.

## If these files move

`conftest.py` adds `tests/unit/../..` to `sys.path` so `import unisi` (and
`import config` for `fixtures_app`) resolve. If you place this directory
somewhere other than `<repo_root>/tests/unit/`, adjust `UNISI_ROOT` near the
top of `conftest.py` accordingly.
