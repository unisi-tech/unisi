"""
Tests for unisi/autotest.py: check_block/check_module (screen-structure
validation), Recorder (records a live session to a JSON fixture),
test() (replays a JSON fixture against a real user), run_tests()
(orchestrates both, plus custom @test functions), and the toolbar-button
handlers that drive Recorder from a UI click.

Test-design split:
  * check_block/check_module are pure functions over Block/Screen/Unit
    objects -- TestCheckBlock/TestCheckModule construct those directly, no
    fixtures_app needed (a lightweight _FakeModule stands in for the real
    imported module object check_module expects, since it only ever reads
    .screen and .__file__ off it).
  * Recorder/test()/run_tests()/the button handlers all need a real User
    against real screens on disk, reading/writing real files under the
    `autotest` directory -- these use the new_user/deliver/app_client
    fixtures from conftest.py, the same "real objects over mocks"
    philosophy tests/core uses for server.py.

Several bugs found while writing this suite are fixed alongside the tests
that pin the corrected behavior (full rationale in the comments at each
fix site in autotest.py/server.py):

  * autotest.py's test() opened its JSON fixture file with a bare open()
    and no close()/with -- a real (if minor) resource leak. See
    TestAutotestFileFunction.test_fixture_file_is_properly_closed.
  * test()'s diff-printing loop assumed every diff node was a dict it
    could call .get('_message') on. A root-level type mismatch (expected
    and actual are entirely different types) produces a flat explain()
    dict instead of one keyed by field name, and used to crash with
    AttributeError trying to iterate it as if it were the latter -- see
    TestDiffPrintingRobustness.
  * server.py's websocket_handler called user.prepare_result() a SECOND
    time (persist=False) to build recorder.accept()'s response, after
    send() already called it once (persist=True) on the very same raw
    result. prepare_result() unconditionally drains changed_units/
    touched_units before returning (documented on the method itself), so
    the second call silently lost all update information for exactly the
    common case (a handler returning None, with the real change tracked
    via changed_units) that autotest's recorder exists to capture --
    recorded fixtures used to capture `null` where the client actually
    received a real update message. See
    TestRecorderServerIntegration.test_recorded_response_matches_what_the_client_actually_received.
"""
import asyncio
import json
import shutil

import pytest

import unisi.autotest as autotest_mod
from unisi.autotest import (
    check_block,
    check_module,
    create_test,
    ask_create_test,
    button,
    button_clicked,
    recorder,
    rewrite,
    run_tests,
    test as autotest_test,
    test_name,
)
from unisi.common import ArgObject, ReceivedMessage, Unishare, toJson
from unisi.containers import Block, Screen
from unisi.server import test as register_test
from unisi.units import Button, Chart, Edit, Unit

from conftest import AUTOTEST_DIR, FIXTURES_APP


class _FakeModule:
    """Minimal stand-in for the module object check_module expects: it
    only ever reads `.screen` and `.__file__` off it (for error messages),
    never anything else, so a real imported module isn't needed."""

    def __init__(self, screen, file="fake_screen.py"):
        self.screen = screen
        self.__file__ = file


def _valid_screen():
    save = Button("Save")
    root = Block("Root", save)
    screen = Screen("Home")
    screen.blocks = [root]
    screen.toolbar = []
    return screen


class TestCheckBlock:
    def test_valid_block_has_no_errors(self):
        block = Block("Root", Button("Save"), Edit("Plain", "x"))
        assert check_block(block, {}) == []

    def test_missing_block_name_reports_error_and_resets_to_unknown(self):
        block = Block("", Button("Save"))
        errors = check_block(block, {})
        assert len(errors) == 1
        assert "does not contain name" in errors[0]
        assert block.name == "Unknown"

    def test_non_string_block_name_reports_error_without_resetting(self):
        block = Block(123, Button("Save"))
        errors = check_block(block, {})
        assert len(errors) == 1
        assert "is not a string" in errors[0]
        assert block.name == 123  # left untouched, unlike the missing-name case

    def test_duplicate_element_names_report_error(self):
        block = Block("Root", Edit("Same", "a"), Edit("Same", "b"))
        errors = check_block(block, {})
        assert any("duplicated element name" in e for e in errors)

    def test_line_type_elements_are_exempt_from_duplicate_name_check(self):
        block = Block("Root", Unit("Same", type="line"), Unit("Same", type="line"))
        assert check_block(block, {}) == []

    def test_invalid_non_unit_element_reports_error_and_does_not_crash(self):
        """Regression test for commit 6040e5d: check_block used to try
        child.name/child.type on a non-Unit element right after flagging
        it, crashing with AttributeError; the `continue` fixes that. This
        test would crash outright if that fix ever regressed."""
        block = Block("Root", "not-a-unit-object", Button("Save"))
        errors = check_block(block, {})
        assert any("instead of Unit+ object" in e for e in errors)
        # the *valid* element after the bad one must still be checked normally
        assert len(errors) == 1

    def test_reused_element_across_the_same_check_reports_error(self):
        shared = Button("Shared")
        block = Block("Root", Block("Inner", shared), shared)
        hash_elements = {}
        errors = check_block(block, hash_elements)
        assert any("already used" in e for e in errors)

    def test_nested_block_errors_propagate_up(self):
        inner = Block("", Button("Save"))  # missing name -> 1 error
        outer = Block("Outer", inner)
        errors = check_block(outer, {})
        assert any("does not contain name" in e for e in errors)

    def test_chart_without_view_or_option_reports_error(self):
        raw_chart = Unit("MyChart", type="chart")  # bypasses the Chart class's own defaults
        block = Block("Root", raw_chart)
        errors = check_block(block, {})
        assert any('"view" or "option" does not defined' in e for e in errors)

    def test_chart_constructed_normally_has_no_error(self):
        """The Chart class itself always sets .option (defaulting to {} if
        not given), so a normally-constructed Chart never trips this
        check -- only a hand-built Unit with type='chart' can, as above."""
        block = Block("Root", Chart("MyChart"))
        assert check_block(block, {}) == []


class TestCheckModule:
    def test_well_formed_screen_has_no_errors(self):
        module = _FakeModule(_valid_screen())
        assert check_module(module) == []

    def test_missing_screen_name_reports_error_and_resets_to_unknown(self):
        screen = _valid_screen()
        screen.name = ""
        module = _FakeModule(screen)
        errors = check_module(module)
        assert any("does not contain name" in e for e in errors)
        assert screen.name == "Unknown"

    def test_non_string_screen_name_reports_error(self):
        screen = _valid_screen()
        screen.name = 123
        module = _FakeModule(screen)
        errors = check_module(module)
        assert any("is not a string" in e for e in errors)

    def test_non_list_blocks_reports_error_and_skips_further_checks(self):
        """Documents current behavior: when `blocks` isn't a list/tuple,
        check_module reports just the one error and does not attempt to
        check the toolbar or scan for blocks at all -- both live inside
        the same `else` branch as the per-block loop. In the normal
        User.compile_screen flow this is unreachable (modules.py already
        wraps a non-list `blocks` into a single-element list before
        check_module ever runs), but check_module itself is a plain
        function that can be, and here is, called directly."""
        screen = _valid_screen()
        screen.blocks = Block("Root", Button("Save"))  # a single Block, not a list
        # a toolbar that WOULD itself report a duplicate-name error if it
        # were ever reached, so its absence below is meaningful evidence
        # that toolbar checking didn't run (not just that it found nothing).
        screen.toolbar = [Edit("Dup", "a"), Edit("Dup", "b")]
        module = _FakeModule(screen)
        errors = check_module(module)
        assert len(errors) == 2  # header line + the one blocks-type error
        assert "'blocks' has to be a list or tuple" in errors[1]
        assert not any("duplicated element name" in e for e in errors)

    def test_duplicate_block_names_report_error(self):
        screen = _valid_screen()
        screen.blocks = [Block("Same", Button("A")), Block("Same", Button("B"))]
        module = _FakeModule(screen)
        errors = check_module(module)
        assert any("duplicated block name" in e for e in errors)

    def test_non_block_element_in_blocks_reports_error(self):
        """Regression test mirroring commit 6040e5d's check_block fix, one
        level up: check_module used to call check_block(bl, ...)
        unconditionally after the isinstance(bl, Block) check, crashing
        with AttributeError for a non-Block element instead of just
        flagging it and moving on."""
        screen = _valid_screen()
        screen.blocks = ["not-a-block", Block("Root", Button("Save"))]
        module = _FakeModule(screen)
        errors = check_module(module)
        assert any("instead of Block object" in e for e in errors)
        # the *valid* block after the bad one must still be checked normally
        assert not any("Root" in e and "duplicated" in e for e in errors)

    def test_toolbar_errors_are_included(self):
        screen = _valid_screen()
        screen.toolbar = [Edit("Same", "a"), Edit("Same", "b")]
        module = _FakeModule(screen)
        errors = check_module(module)
        assert any("duplicated element name" in e for e in errors)

    def test_errors_are_prefixed_with_a_header_line(self):
        screen = _valid_screen()
        screen.name = ""
        module = _FakeModule(screen, file="my_screen.py")
        errors = check_module(module)
        assert "Errors in screen" in errors[0]
        assert "my_screen.py" in errors[0]

    def test_no_errors_means_no_header_line(self):
        module = _FakeModule(_valid_screen())
        assert check_module(module) == []


class TestAutotestFileFunction:
    """The module-level test(filename, user) function: reads a JSON
    fixture of alternating (message, expected) pairs from the `autotest`
    directory and replays each message against a real user."""

    def _write_fixture(self, fname, pairs):
        AUTOTEST_DIR.mkdir(exist_ok=True)
        (AUTOTEST_DIR / fname).write_text(json.dumps(pairs))

    def test_passes_when_actual_matches_expected(self, new_user):
        user = new_user()
        self._write_fixture("passing.json", [
            {"block": "Root", "element": "Save", "event": "changed", "value": "clicked"},
            {"type": "update", "updates": [
                {"data": {"name": "Result", "value": "saved:clicked", "x": 0, "type": "string"},
                 "path": ["Result", "Root"]},
            ]},
        ])
        assert autotest_test("passing.json", user) is True

    def test_fails_and_prints_diff_when_mismatched(self, new_user, capsys):
        user = new_user()
        self._write_fixture("failing.json", [
            {"block": "Root", "element": "Save", "event": "changed", "value": "clicked"},
            {"type": "update", "updates": [
                {"data": {"name": "Result", "value": "THIS-IS-WRONG", "x": 0, "type": "string"},
                 "path": ["Result", "Root"]},
            ]},
        ])
        ok = autotest_test("failing.json", user)
        out = capsys.readouterr().out
        assert ok is False
        assert "failing.json is failed" in out
        assert "Values not equal" in out

    def test_multiple_pairs_all_get_checked(self, new_user):
        user = new_user()
        self._write_fixture("multi.json", [
            {"block": "Root", "element": "Plain", "event": "changed", "value": "x"},
            None,
            {"block": "Root", "element": "Save", "event": "changed", "value": "y"},
            {"type": "update", "updates": [
                {"data": {"name": "Result", "value": "saved:y", "x": 0, "type": "string"},
                 "path": ["Result", "Root"]},
            ]},
        ])
        assert autotest_test("multi.json", user) is True

    def test_fixture_file_is_properly_closed(self, new_user, monkeypatch):
        """Regression test: test() used to `open()` its JSON fixture file
        without a `with` block or explicit .close() -- a real resource
        leak. Wraps builtins.open to capture the specific file object
        opened for the fixture path and assert it ends up closed."""
        user = new_user()
        fname = "close_check.json"
        self._write_fixture(fname, [
            {"block": "Root", "element": "Plain", "event": "changed", "value": "x"},
            None,
        ])
        fixture_path = f"{autotest_mod.testdir}{autotest_mod.divpath}{fname}"
        opened = []
        real_open = open

        def tracking_open(file, *args, **kwargs):
            f = real_open(file, *args, **kwargs)
            if str(file) == fixture_path:
                opened.append(f)
            return f

        monkeypatch.setattr("builtins.open", tracking_open)
        autotest_test(fname, user)

        assert len(opened) == 1
        assert opened[0].closed is True


class TestDiffPrintingRobustness:
    """Regression tests for test()'s diff-printing loop, isolated from
    real message-passing by monkeypatching the module-level `comparator`
    so the diff shape can be controlled directly -- these specifically
    target the printing loop's robustness to unusual diff shapes, not the
    comparator itself (see tests/jsoncomparison/ for that)."""

    def _run_with_fake_comparator(self, new_user, fake_diff, monkeypatch):
        user = new_user()
        AUTOTEST_DIR.mkdir(exist_ok=True)
        fname = "fake_diff.json"
        (AUTOTEST_DIR / fname).write_text(json.dumps([
            {"block": "Root", "element": "Plain", "event": "changed", "value": "x"},
            {"whatever": "the real comparator is replaced below"},
        ]))
        monkeypatch.setattr(autotest_mod, "comparator", lambda e, a: fake_diff)
        return autotest_test(fname, user)

    def test_root_level_type_mismatch_does_not_crash(self, new_user, monkeypatch, capsys):
        """Before the fix, diff.items() was iterated as if every value were
        a nested diff dict; for a flat explain() dict (a root-level type
        mismatch, expected/actual entirely different types) this meant
        obj was a plain string (e.g. the _message text) on the first
        iteration, and obj.get(...) raised AttributeError."""
        fake_diff = {
            "_message": "Types not equal. Expected: <NoneType>, received: <dict>",
            "_expected": "NoneType",
            "_received": "dict",
        }
        ok = self._run_with_fake_comparator(new_user, fake_diff, monkeypatch)
        out = capsys.readouterr().out
        assert ok is False
        assert "Types not equal" in out

    def test_none_shaped_diff_node_does_not_crash(self, new_user, monkeypatch, capsys):
        """Mirrors the exact diff shape the pre-fix _list_diff bug used to
        produce (see tests/jsoncomparison/test_jsoncomparison.py's
        regression test for the root cause) -- kept here too as defense in
        depth for the printing loop itself, independent of whether
        jsoncomparison could ever produce it again."""
        fake_diff = {"items": {"_content": {1: None}}}
        ok = self._run_with_fake_comparator(new_user, fake_diff, monkeypatch)
        out = capsys.readouterr().out
        assert ok is False
        assert "failed on message" in out


class TestRecorder:
    def test_initial_state(self):
        r = autotest_mod.Recorder()
        assert r.record_file is None
        assert r.record_buffer == []

    def test_start_with_falsy_fname_does_not_record(self):
        r = autotest_mod.Recorder()
        r.start(None)
        assert r.record_file is None
        assert r.record_buffer == []

    def test_start_records_a_bootstrap_entry(self, new_user):
        user = new_user()
        autotest_mod.User.last_user = user
        r = autotest_mod.Recorder()
        r.start("autotest/probe.json")
        assert r.record_file == "autotest/probe.json"
        assert len(r.record_buffer) == 1
        assert '"root"' in r.record_buffer[0] or "'root'" in r.record_buffer[0]

    def test_the_message_that_triggered_start_is_skipped_not_recorded(self, new_user):
        """Pins Recorder's own bootstrap bookkeeping: the very next accept()
        call after start() represents the dialog's own 'Ok' click that
        triggered recording in the first place (see server.py's
        websocket_handler -- start() runs *during* that message's
        handling, then the outer loop calls accept() for that same
        message once handling returns) and must be dropped, not recorded."""
        user = new_user()
        autotest_mod.User.last_user = user
        r = autotest_mod.Recorder()
        r.start("autotest/probe.json")
        assert len(r.record_buffer) == 1

        r.accept(ArgObject(block="Root", element="OkButton", event="changed", value="ok"), "noise")
        assert len(r.record_buffer) == 1, "the message that started recording must not itself be recorded"

    def test_subsequent_real_messages_are_recorded(self, new_user):
        user = new_user()
        autotest_mod.User.last_user = user
        r = autotest_mod.Recorder()
        r.start("autotest/probe.json")
        r.accept(ArgObject(block="Root", element="OkButton", event="changed", value="ok"), "noise")  # skipped

        r.accept(ArgObject(block="Root", element="Save", event="changed", value="real"), {"type": "update"})
        assert len(r.record_buffer) == 2
        assert "real" in r.record_buffer[1]

    def test_stop_recording_with_only_the_bootstrap_entry_reports_nothing_to_save(self, new_user, tmp_path):
        user = new_user()
        autotest_mod.User.last_user = user
        r = autotest_mod.Recorder()
        target = tmp_path / "probe.json"
        r.start(str(target))

        info = r.stop_recording(None, "Ok")
        assert info.type == "warning"
        assert "Nothing to save" in info.value
        assert not target.exists()
        assert r.record_file is None

    def test_stop_recording_writes_a_valid_json_array(self, new_user, tmp_path):
        user = new_user()
        autotest_mod.User.last_user = user
        r = autotest_mod.Recorder()
        target = tmp_path / "probe.json"
        r.start(str(target))
        r.accept(ArgObject(block="Root", element="OkButton", event="changed", value="ok"), "noise")
        r.accept(ArgObject(block="Root", element="Save", event="changed", value="real"), {"type": "update"})

        info = r.stop_recording(None, "Ok")
        assert info.type == "info"
        assert "is created" in info.value
        assert target.exists()

        data = json.loads(target.read_text())
        assert len(data) == 4  # 2 (message, response) pairs
        assert data[2]["element"] == "Save"
        assert data[3] == {"type": "update"}


class TestButtonHandlers:
    def test_button_clicked_returns_a_dialog_and_creates_testdir(self, new_user):
        if AUTOTEST_DIR.exists():
            shutil.rmtree(AUTOTEST_DIR)
        user = new_user()
        autotest_mod.User.last_user = user

        result = button_clicked(None, None)

        assert result.type == "dialog"
        assert result.name == "Create autotest.."
        assert AUTOTEST_DIR.exists()
        assert test_name.value == user.screen.name

    def test_ask_create_test_with_no_filename_warns(self, new_user):
        user = new_user()
        autotest_mod.User.last_user = user
        test_name.value = ""

        result = ask_create_test(None, "Ok")

        assert result.type == "warning"
        assert "not defined" in result.value

    def test_ask_create_test_cancel_does_nothing(self, new_user):
        user = new_user()
        autotest_mod.User.last_user = user
        test_name.value = "irrelevant.json"

        assert ask_create_test(None, "Cancel") is None
        assert recorder.record_file is None

    def test_ask_create_test_ok_with_a_filename_starts_recording(self, new_user):
        user = new_user()
        autotest_mod.User.last_user = user
        test_name.value = "started.json"
        rewrite.value = False

        result = ask_create_test(None, "Ok")

        assert result.type == "info"
        assert "recording" in result.value
        assert recorder.record_file == f"autotest/started.json"
        assert button.spinner is True

        recorder.stop_recording(None, "Ok")  # leave the button/recorder in a clean state

    def test_create_test_refuses_to_overwrite_without_rewrite_flag(self, new_user):
        AUTOTEST_DIR.mkdir(exist_ok=True)
        (AUTOTEST_DIR / "existing.json").write_text("[]")
        user = new_user()
        autotest_mod.User.last_user = user
        rewrite.value = False

        result = create_test("existing.json")

        assert result.type == "warning"
        assert "already exists" in result.value
        assert recorder.record_file is None

    def test_create_test_overwrites_when_rewrite_flag_is_set(self, new_user):
        AUTOTEST_DIR.mkdir(exist_ok=True)
        (AUTOTEST_DIR / "existing.json").write_text("[]")
        user = new_user()
        autotest_mod.User.last_user = user
        rewrite.value = True

        result = create_test("existing.json")

        assert result.type == "info"
        recorder.stop_recording(None, "Ok")


class TestRunTests:
    def test_screen_definitions_correct_message_is_printed_for_a_well_formed_app(self, new_user, capsys):
        user = new_user()
        run_tests(user)
        assert "screen definitions are correct" in capsys.readouterr().out

    def test_malformed_screen_errors_are_printed_and_reactivity_is_skipped(self, new_user, capsys):
        user = new_user()
        good_module = user.screens[0]
        good_module.screen._mark_changed = None  # so we can tell run_tests is the one that (re)sets it

        bad_screen = Screen("Bad")
        bad_screen.blocks = "not-a-list"
        bad_screen.toolbar = []
        bad_module = _FakeModule(bad_screen, file="bad_screen.py")

        user.screens = [good_module, bad_module]
        run_tests(user)
        out = capsys.readouterr().out

        assert "Detected errors" in out
        assert "'blocks' has to be a list or tuple" in out
        assert good_module.screen._mark_changed is not None, "a well-formed screen must still get reactivity"
        assert bad_module.screen._mark_changed is None, "a screen with structural errors must not get reactivity"

    def test_autotest_disabled_does_not_process_files_even_if_present(self, new_user, capsys):
        """Regression pin for an already-fixed bug (see the comment at
        `files = config.autotest` in autotest.py): `file in files` on a
        bool used to raise TypeError whenever config.autotest was False
        but the `autotest` directory already existed from an earlier
        recording."""
        import config
        AUTOTEST_DIR.mkdir(exist_ok=True)
        (AUTOTEST_DIR / "somefile.json").write_text("[]")
        config.autotest = False

        user = new_user()
        run_tests(user)  # must not raise

        assert "Autotests successfully passed" not in capsys.readouterr().out

    def test_subdirectory_inside_testdir_is_skipped_without_crashing(self, new_user):
        """Regression pin: a subdirectory inside `autotest/` used to be
        checked against the CWD instead of the full path, so it could pass
        the "is this a directory" guard by accident and crash test() with
        IsADirectoryError trying to open() it."""
        import config
        AUTOTEST_DIR.mkdir(exist_ok=True)
        (AUTOTEST_DIR / "subdir").mkdir()
        config.autotest = "*"

        user = new_user()
        run_tests(user)  # must not raise

    def test_star_processes_every_file_in_testdir(self, new_user, capsys):
        import config
        AUTOTEST_DIR.mkdir(exist_ok=True)
        pair = [
            {"block": "Root", "element": "Plain", "event": "changed", "value": "x"},
            None,
        ]
        (AUTOTEST_DIR / "a.json").write_text(json.dumps(pair))
        (AUTOTEST_DIR / "b.json").write_text(json.dumps(pair))
        config.autotest = "*"

        user = new_user()
        run_tests(user)

        assert "Autotests successfully passed" in capsys.readouterr().out

    def test_explicit_file_list_processes_only_those_files(self, new_user, capsys):
        import config
        AUTOTEST_DIR.mkdir(exist_ok=True)
        passing = [
            {"block": "Root", "element": "Plain", "event": "changed", "value": "x"},
            None,
        ]
        failing = [
            {"block": "Root", "element": "Plain", "event": "changed", "value": "x"},
            {"type": "this-will-never-match"},
        ]
        (AUTOTEST_DIR / "included.json").write_text(json.dumps(passing))
        (AUTOTEST_DIR / "excluded.json").write_text(json.dumps(failing))
        config.autotest = ["included.json"]

        user = new_user()
        run_tests(user)

        assert "Autotests successfully passed" in capsys.readouterr().out

    def test_sync_custom_test_function_runs(self, new_user):
        calls = []
        register_test(lambda: calls.append("sync"))

        user = new_user()
        run_tests(user)

        assert calls == ["sync"]

    def test_async_custom_test_function_runs(self, new_user):
        calls = []

        async def my_test():
            calls.append("async")

        register_test(my_test)
        user = new_user()
        run_tests(user)

        assert calls == ["async"]

    def test_custom_test_function_exception_is_logged_not_raised(self, new_user):
        def failing():
            raise ValueError("boom")

        register_test(failing)
        logs = []
        user = new_user()
        user.log = lambda msg: logs.append(msg)

        run_tests(user)  # must not raise

        assert any("boom" in msg for msg in logs)


class TestRecorderServerIntegration:
    """The Recorder/server.py integration: server.py's websocket_handler is
    the only caller of recorder.accept(), and the bug this pins lived
    entirely in *how* it called send()/prepare_result()/recorder.accept()
    together -- not in Recorder or prepare_result individually. A real
    aiohttp TestClient websocket connection is used deliberately (rather
    than calling Recorder/prepare_result directly) so this test would have
    failed against the actual pre-fix code, the same way tests/core/
    test_server.py exercises websocket_handler for its own regressions.
    """

    @pytest.mark.asyncio
    async def test_recorded_response_matches_what_the_client_actually_received(self, app_client):
        AUTOTEST_DIR.mkdir(exist_ok=True)  # normally done by button_clicked(), bypassed here
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()  # initial screen

            # Start recording (bypassing the dialog UI for directness --
            # button_clicked/create_test's own dialog flow is covered
            # separately in TestButtonHandlers).
            recorder.start("autotest/roundtrip.json")

            # The very next message after start() is always treated by
            # Recorder.accept's ignored_1message bookkeeping as the "Ok"
            # click that triggered recording in the first place (see
            # server.py's websocket_handler: start() runs *during* that
            # message's own handling, then the outer loop calls accept()
            # for that same message once handling returns) and is never
            # recorded. Send a throwaway message to stand in for it.
            await ws.send_str(toJson({
                "block": "Root", "element": "Plain", "event": "changed", "value": "throwaway-start-noise",
            }))
            await ws.receive_str()

            await ws.send_str(toJson({
                "block": "Root", "element": "Save", "event": "changed", "value": "clicked",
            }))
            real_reply = json.loads(await ws.receive_str())

            info = recorder.stop_recording(None, "Ok")
            assert info.type == "info"

        recorded = json.loads((AUTOTEST_DIR / "roundtrip.json").read_text())
        # entries: [bootstrap_msg, bootstrap_resp, save_msg, save_resp]
        assert len(recorded) == 4
        recorded_response = recorded[3]

        assert recorded_response == real_reply
        assert recorded_response is not None
        assert recorded_response.get("type") == "update"

    @pytest.mark.asyncio
    async def test_recorded_fixture_replays_successfully(self, app_client, new_user):
        AUTOTEST_DIR.mkdir(exist_ok=True)
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()
            recorder.start("autotest/roundtrip2.json")
            await ws.send_str(toJson({
                "block": "Root", "element": "Plain", "event": "changed", "value": "throwaway-start-noise",
            }))
            await ws.receive_str()
            await ws.send_str(toJson({
                "block": "Root", "element": "Save", "event": "changed", "value": "clicked-for-replay",
            }))
            await ws.receive_str()
            recorder.stop_recording(None, "Ok")

        replay_user = new_user(session="autotest")
        # autotest_test() calls asyncio.run() internally (fine when called
        # synchronously, as run_tests() does in production) -- this test
        # itself runs inside pytest-asyncio's own event loop, where a
        # nested asyncio.run() would raise RuntimeError, so it's run in a
        # separate thread instead.
        assert await asyncio.to_thread(autotest_test, "roundtrip2.json", replay_user) is True
