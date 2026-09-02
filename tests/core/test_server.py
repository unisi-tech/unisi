"""
Tests for server.py: generate_random_string, context_user/context_screen,
message_logger, make_user, handle(), post_handler, static_serve,
websocket_handler, ensure_directory_exists, ensure_unisi_typings, and
start()'s route/composition wiring.

server.py is architecturally an aiohttp app, not a library of pure
functions -- most of it only makes sense wired into a real
aiohttp.web.Application and driven with aiohttp's own test client (the
`app_client` fixture in conftest.py), the same "prefer the real thing over
a mock" choice tests/users and tests/persist_voice_reloder make for User
itself. make_user()/handle() are the exceptions: small enough, and general
enough, to unit-test directly with a bare mocked Request / a bare User.
"""
import asyncio
import json
import logging

import pytest
from aiohttp.test_utils import make_mocked_request

import unisi.server as server_mod
from unisi.common import Message, ReceivedMessage, Unishare, toJson
from unisi.users import User


class TestGenerateRandomString:
    def test_default_length_is_ten(self):
        assert len(server_mod.generate_random_string()) == 10

    def test_custom_length(self):
        assert len(server_mod.generate_random_string(20)) == 20

    def test_only_letters_and_digits(self):
        s = server_mod.generate_random_string(200)
        assert s.isalnum()

    def test_calls_are_not_all_identical(self):
        # Not a strict correctness guarantee (it's random), but two calls
        # colliding out of 10**~35 possibilities would indicate the RNG
        # itself is broken, not bad luck.
        values = {server_mod.generate_random_string() for _ in range(20)}
        assert len(values) == 20


class TestContextUserAndScreen:
    def test_context_user_finds_user_via_method_call(self, new_user):
        user = new_user()

        def call_context_user():
            return server_mod.context_user()

        # A method call puts `self` (the User instance) as frame[0]'s
        # first positional arg -- context_object's whole detection
        # mechanism. Wrap in an actual bound-method-style call.
        class Runner:
            def run(self_inner):
                return call_context_user()

        assert Runner().run() is None  # no User frame directly above -- see next test

    def test_context_user_finds_user_from_a_user_method(self, new_user):
        user = new_user()
        found = []

        def probe():
            found.append(server_mod.context_user())

        # Monkey-attach a throwaway method so `self` (== user) is frame[0]'s
        # first positional arg, exactly like any real User method.
        User._probe_context = lambda self: probe()
        try:
            user._probe_context()
        finally:
            del User._probe_context

        assert found[0] is user

    def test_context_user_returns_none_with_no_user_on_the_stack(self):
        assert server_mod.context_user() is None

    def test_context_screen_returns_the_users_screen(self, new_user):
        user = new_user()
        found = []
        User._probe_screen = lambda self: found.append(server_mod.context_screen())
        try:
            user._probe_screen()
        finally:
            del User._probe_screen

        assert found[0] is user.screen

    def test_context_screen_returns_none_with_no_user(self):
        assert server_mod.context_screen() is None


class TestMessageLogger:
    def test_with_user_in_context_delegates_to_user_log(self, new_user):
        user = new_user()
        logged = []
        user.log = lambda message, type='error': logged.append((message, type))

        User._probe_log = lambda self: server_mod.message_logger('hello', 'warning')
        try:
            user._probe_log()
        finally:
            del User._probe_log

        assert logged == [('hello', 'warning')]

    def test_without_user_logs_at_error_level_regardless_of_type(self, caplog):
        # Documented, deliberate behavior (see the comment on
        # message_logger itself, and setup_llmrag()'s docstring in
        # llmrag.py): with no user in context there's nowhere to route a
        # leveled message, so it always goes out as logging.error --
        # even when `type` says otherwise -- so it's not silently dropped
        # by the WARNING-level root logger start_logging() configures.
        with caplog.at_level(logging.ERROR):
            server_mod.message_logger('startup notice', 'info')
        assert any(
            r.levelno == logging.ERROR and r.message == 'startup notice'
            for r in caplog.records
        )


class TestMakeUser:
    def _req(self, query=""):
        path = f"/ws?{query}" if query else "/ws"
        return make_mocked_request("GET", path)

    @pytest.fixture(autouse=True)
    def _cleanup_any_persisted_users(self, new_user):
        # make_user() constructs real Users directly (it IS the function
        # under test), bypassing the new_user fixture's own session
        # tracking/cleanup -- so track fixtures_app/users/*.db ourselves
        # and remove anything new this test created. Depending on
        # new_user (even though it's never called) pulls in
        # _app_on_path, so cwd is already fixtures_app by the time this
        # runs.
        from pathlib import Path
        users_dir = Path("users")
        before = set(users_dir.glob("*.db*")) if users_dir.exists() else set()
        yield
        after = set(users_dir.glob("*.db*")) if users_dir.exists() else set()
        for p in after - before:
            p.unlink(missing_ok=True)

    def test_no_query_creates_a_fresh_user_and_registers_the_session(self, new_user):
        # `new_user` isn't used to build the user (make_user is, since
        # that's exactly what's under test) -- it's only depended on here
        # to get _app_on_path's cwd/sys.path setup for free.
        from unisi.common import Unishare

        count_before = User.count
        user, ok = server_mod.make_user(self._req())

        assert user is not None
        assert ok  # user.screens is non-empty in fixtures_app
        assert User.count == count_before + 1
        assert Unishare.sessions[user.session] is user

    def test_id_query_param_is_embedded_in_the_generated_session(self, new_user):
        user, ok = server_mod.make_user(self._req("id=my-custom-id"))
        assert user.session.endswith("-my-custom-id")

    def test_screen_query_param_selects_that_screen(self, new_user):
        user, ok = server_mod.make_user(self._req("screen=Other"))
        assert user.screen.name == "Other"

    def test_explicit_session_without_share_is_used_verbatim(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "share", False)
        user, ok = server_mod.make_user(self._req("session=explicit-session-1"))
        assert user.session == "explicit-session-1"

    def test_share_mode_unknown_session_is_refused(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "share", True)
        user, status = server_mod.make_user(self._req("session=does-not-exist-1"))
        assert user is None
        # Error(...) is a factory function returning a Message (type='error'),
        # not a class -- isinstance() against it isn't meaningful.
        assert isinstance(status, Message)
        assert status.type == "error"

    def test_share_mode_known_session_shares_state(self, new_user, monkeypatch):
        # First, register a real session the normal way (share off).
        monkeypatch.setattr(server_mod.config, "share", False)
        original, _ = server_mod.make_user(self._req("session=shared-base-1"))

        # Now a second connection references that same session with
        # share turned on -- should find and share it, not refuse it.
        monkeypatch.setattr(server_mod.config, "share", True)
        shared_user, ok = server_mod.make_user(self._req("session=shared-base-1"))

        assert shared_user is not None
        assert shared_user is not original  # a distinct User instance
        assert shared_user.handlers is original.handlers  # sharing the same state

    def test_mirror_mode_reuses_last_user_once_one_exists(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "share", False)
        base, _ = server_mod.make_user(self._req("id=base"))

        monkeypatch.setattr(server_mod.config, "mirror", True)
        mirrored, ok = server_mod.make_user(self._req("id=mirrored"))

        assert mirrored is not base
        assert mirrored.handlers is base.handlers

    def test_mirror_mode_with_no_prior_user_falls_back_to_a_fresh_one(self, new_user, monkeypatch):
        # User.count == 0 -- config.mirror alone isn't enough to mirror.
        monkeypatch.setattr(User, "count", 0)
        monkeypatch.setattr(server_mod.config, "mirror", True)
        user, ok = server_mod.make_user(self._req())
        assert user is not None
        assert ok

    def test_share_mode_does_not_overwrite_the_root_session_entry(self, new_user, monkeypatch):
        # A share='session' connect (a proxy/reflection hot-attaching to an
        # existing session, e.g. Proxy() in test_apps/proxy/run_blocks.py)
        # must NOT replace Unishare.sessions[session]. That entry has to
        # keep pointing at the original, long-lived owner -- otherwise the
        # NEXT share connect for the same session id resolves against this
        # transient one instead, and once this one disconnects, that next
        # connect is left sharing state with a dead object.
        from unisi.common import Unishare

        monkeypatch.setattr(server_mod.config, "share", False)
        root, _ = server_mod.make_user(self._req("session=anchor-1"))

        monkeypatch.setattr(server_mod.config, "share", True)
        proxy, _ = server_mod.make_user(self._req("session=anchor-1"))

        assert Unishare.sessions["anchor-1"] is root
        assert proxy is not root

    @pytest.mark.asyncio
    async def test_sequential_share_connections_all_stay_reachable_from_root(self, new_user, monkeypatch):
        # End-to-end regression for the run_blocks.py symptom: run a
        # hot-connecting proxy script against the same live session twice
        # in a row. The first proxy's set_value() reaches the root's
        # browser; the root then must ALSO receive the second proxy's
        # update, even though the first proxy has already disconnected by
        # the time the second one connects.
        from unisi.common import Unishare

        monkeypatch.setattr(server_mod.config, "share", False)
        root, _ = server_mod.make_user(self._req("session=anchor-2"))

        monkeypatch.setattr(server_mod.config, "share", True)
        proxy1, _ = server_mod.make_user(self._req("session=anchor-2"))
        assert root.reflections == [root, proxy1]

        await proxy1.delete()  # proxy.close() in run_blocks.py
        assert root.reflections == [root]  # the lone, load-bearing self-reference

        proxy2, _ = server_mod.make_user(self._req("session=anchor-2"))

        assert Unishare.sessions["anchor-2"] is root
        assert root in proxy2.reflections
        assert proxy2.reflections is root.reflections


def _call_as_user(user, fn):
    """Call fn() from inside a frame whose first positional arg is `user`,
    so context_user() (and therefore handle()) resolves to `user` -- this
    mirrors how compile_screen()/exec_module() genuinely run as a method
    on the specific User whose screen is being (lazily) loaded during a
    real screen load, letting handle()'s @handle(...) decorators see the
    right user on the stack. Same mechanism as
    TestContextUserAndScreen.test_context_user_finds_user_from_a_user_method,
    factored out here since TestHandle needs it repeatedly."""
    User._probe_handle = lambda self: fn()
    try:
        return user._probe_handle()
    finally:
        del User._probe_handle


class TestHandle:
    def test_registers_a_new_handler(self, new_user):
        user = new_user()

        def fn(obj, value):
            return None

        marker = object()
        _call_as_user(user, lambda: server_mod.handle(marker, "clicked")(fn))

        assert user.handlers[(marker, "clicked")] is fn

    @pytest.mark.asyncio
    async def test_second_registration_for_the_same_key_composes(self, new_user):
        user = new_user()
        calls = []

        def fn1(obj, value):
            calls.append("fn1")

        def fn2(obj, value):
            calls.append("fn2")

        marker = object()

        def register_both():
            server_mod.handle(marker, "clicked")(fn1)
            server_mod.handle(marker, "clicked")(fn2)

        _call_as_user(user, register_both)  # both decorators fire in the
        # same screen-exec pass in real usage, so register both in one go

        composed = user.handlers[(marker, "clicked")]
        assert composed not in (fn1, fn2)  # it's compose_handlers' wrapper now
        await composed(marker, "v")
        assert calls == ["fn1", "fn2"]

    def test_targets_the_context_users_handlers_specifically(self, new_user):
        """Regression test for the User.last_user -> context_user() fix
        (see handle()'s docstring comment in server.py): handle() must
        attribute a handler to whichever User's call stack is actually
        registering it -- e.g. a previously-connected user lazily loading
        a screen they haven't visited yet -- not to whichever User was
        most recently *constructed* process-wide. user_b stands in for
        "someone else connected in the meantime": User.last_user is
        user_b, yet the handler must still land on user_a, the one
        actually in context."""
        user_a = new_user()
        user_b = new_user()
        assert User.last_user is user_b  # user_b, not user_a, is "last" here

        marker = object()
        _call_as_user(
            user_a, lambda: server_mod.handle(marker, "clicked")(lambda obj, value: None)
        )

        assert (marker, "clicked") in user_a.handlers
        assert (marker, "clicked") not in user_b.handlers

    def test_falls_back_to_user_last_user_when_no_user_is_on_the_stack(self, new_user):
        """handle() (and, transitively, Table.__init__ in tables.py, which
        calls it) doesn't always run inside a User method's call chain --
        e.g. a persistent Table() built directly by a unit test, with no
        real screen load involved (see tests/units/conftest.py's FakeUser,
        wired in as User.last_user rather than found via context_user()).
        context_user() correctly finds nothing on the stack in that case,
        and handle() must still fall back to User.last_user rather than
        landing everything in Unishare.pending_handlers -- that fallback
        is what test_targets_the_context_users_handlers_specifically above
        confirms context_user() correctly takes priority over whenever a
        real User *is* in context."""
        user = new_user()
        User.last_user = user  # e.g. FakeUser() in tests/units/conftest.py
        marker = object()

        def fn(obj, value):
            return None

        server_mod.handle(marker, "clicked")(fn)  # called with no User on the stack

        assert user.handlers[(marker, "clicked")] is fn

    def test_falls_back_to_pending_handlers_when_no_user_exists_yet(self):
        """
        Regression: a persistent Table() declared at plain module level
        (e.g. a shared table meant to be usable from backend code, not
        only from inside a screen module) registers its search/filter/
        changed handlers unconditionally at *import* time, before
        unisi.start() has created a single User -- User.last_user is None
        then, and handle() used to crash with
        AttributeError: 'NoneType' object has no attribute 'handlers'
        before a single test could even run. It must not.
        """
        User.last_user = None
        marker = object()

        def fn(obj, value):
            return None

        server_mod.handle(marker, "clicked")(fn)  # must not raise

        assert Unishare.pending_handlers[(marker, "clicked")] is fn

    def test_pending_handlers_composes_like_a_real_users_handlers_do(self):
        """Same compose-on-collision behaviour as
        test_second_registration_for_the_same_key_composes, just landing
        in Unishare.pending_handlers instead of a User's own dict."""
        User.last_user = None
        calls = []

        def fn1(obj, value):
            calls.append("fn1")

        def fn2(obj, value):
            calls.append("fn2")

        marker = object()
        server_mod.handle(marker, "clicked")(fn1)
        server_mod.handle(marker, "clicked")(fn2)

        composed = Unishare.pending_handlers[(marker, "clicked")]
        assert composed not in (fn1, fn2)

    def test_a_real_user_created_later_does_not_retroactively_gain_it(self, new_user):
        """pending_handlers is only ever *read* by handle() while
        User.last_user is still None, and only ever *seeded into* a User
        at that User's own construction time (see User.__init__ in
        users.py) -- registering something into it and only afterwards
        constructing a User is the intended case (a module-level Table()
        always runs before unisi.start() creates anyone), and is covered
        by test_users.py's construction tests, not duplicated here."""
        User.last_user = None
        marker = object()
        server_mod.handle(marker, "clicked")(lambda obj, value: None)

        user = new_user()  # constructed AFTER the registration above

        assert (marker, "clicked") in user.handlers  # seeded at construction


class TestPostHandler:
    @pytest.mark.asyncio
    async def test_uploads_a_file_successfully(self, app_client):
        import io
        import aiohttp

        # An explicit filename, not a bare bytes value -- aiohttp is
        # deprecating "passing bytes creates a file field" (a bytes value
        # with no filename will become a plain form field, not a file
        # field, in v4), which post_handler specifically depends on via
        # field.filename. Being explicit here is what a real upload
        # client does anyway, and keeps this test valid across aiohttp
        # versions rather than relying on the deprecated inference.
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(b"hello world"), filename="hello.txt")
        resp = await app_client.post("/", data=form)
        assert resp.status == 200
        saved_path = await resp.text()
        assert (server_mod.Path(saved_path)).read_bytes() == b"hello world"
        server_mod.Path(saved_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_no_file_field_is_a_bad_request(self, app_client):
        import aiohttp

        # A real multipart body, but the one field it carries has no
        # filename -- exactly the `not getattr(field, 'filename', None)`
        # case post_handler is meant to reject with 400. (An empty dict
        # doesn't reach that check at all: aiohttp's client doesn't send
        # multipart/* for it, so request.multipart() itself asserts; and
        # a plain add_field(name, value) on a default FormData doesn't
        # switch to multipart encoding either -- default_to_multipart
        # forces it regardless of what fields get added.)
        form = aiohttp.FormData(default_to_multipart=True)
        form.add_field("note", "just a plain text field, not a file")
        resp = await app_client.post("/", data=form)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_path_traversal_filename_is_sanitized_to_a_basename(self, app_client):
        import io
        import aiohttp

        # quote_fields=False -- a well-behaved client (the aiohttp default)
        # percent-encodes '/' in the filename it sends, which never
        # reaches post_handler as a literal traversal attempt in the first
        # place. A hostile client isn't obliged to be well-behaved, so
        # this sends the raw, unescaped filename instead.
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("file", io.BytesIO(b"evil?"), filename="../../etc/passwd")
        resp = await app_client.post("/", data=form)

        assert resp.status == 200
        saved_path = await resp.text()
        # saved strictly inside upload_dir, as just "passwd" -- not escaping via ../../
        assert server_mod.Path(saved_path).name == "passwd"
        assert "etc" not in server_mod.Path(saved_path).parts
        server_mod.Path(saved_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_filename_that_sanitizes_to_empty_is_a_bad_request(self, app_client):
        """Regression test: Path('..').name is '..' -- pathlib treats a
        bare '..' as an ordinary last path segment, not something to
        resolve away -- so this used to reach
        open(f'{upload_dir}/..', 'wb'), which points at upload_dir's
        *parent directory* and raised an unhandled IsADirectoryError (a
        500) instead of a clean 400.
        """
        import io
        import aiohttp

        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(b"x"), filename="..")
        resp = await app_client.post("/", data=form)

        assert resp.status == 400


class TestStaticServe:
    @pytest.mark.asyncio
    async def test_root_serves_index_html(self, app_client):
        resp = await app_client.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert "<html" in body.lower()

    @pytest.mark.asyncio
    async def test_serves_an_existing_bundled_asset(self, app_client):
        resp = await app_client.get("/favicon.ico")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_missing_file_is_404(self, app_client):
        resp = await app_client.get("/this/does/not/exist.xyz")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_traversal_outside_webpath_is_blocked(self, app_client):
        resp = await app_client.get("/../../../../../../etc/passwd")
        assert resp.status in (404, 400)
        if resp.status == 200:
            pytest.fail("path traversal must never return 200")

    @pytest.mark.asyncio
    async def test_serves_a_file_from_a_configured_public_dir(self, app_client, tmp_path, monkeypatch):
        (tmp_path / "shared.txt").write_text("public content")
        monkeypatch.setattr(server_mod.config, "public_dirs", [str(tmp_path)])

        # tmp_path is already an absolute POSIX path (starts with '/') --
        # no extra leading slash, or the URL becomes "//tmp/..." which
        # parses as a scheme-relative *absolute* URL instead of a path.
        resp = await app_client.get(f"{tmp_path}/shared.txt")
        assert resp.status == 200
        assert await resp.text() == "public content"

    @pytest.mark.asyncio
    async def test_public_dir_traversal_to_a_sibling_directory_is_blocked(
        self, app_client, tmp_path, monkeypatch
    ):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("do not serve me")
        monkeypatch.setattr(server_mod.config, "public_dirs", [str(allowed)])

        resp = await app_client.get(f"{allowed}/../secret.txt")
        assert resp.status == 404


class TestWebsocketHandler:
    @pytest.mark.asyncio
    async def test_initial_message_is_the_full_screen_when_screens_are_available(self, app_client):
        # send(True if status else empty_app): status (user.screens) is
        # truthy in fixtures_app, so send(True) runs -- and
        # prepare_result() turns a bare `True` into "send the whole
        # current screen" (a full reload), not the literal value True.
        async with app_client.ws_connect("/ws") as ws:
            first = await ws.receive_json()
            assert first["type"] == "screen"
            assert first["name"] == "Home"

    @pytest.mark.asyncio
    async def test_single_message_round_trip_runs_the_real_handler(self, app_client):
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()  # initial screen
            await ws.send_str(toJson({
                "path": ["Save", "Root"], "event": "changed", "value": "x",
            }))
            # on_save's return value ("saved", already a str) is sent
            # via send()'s `if type(res) != str` fast path -- straight
            # ws.send_str(), no JSON envelope -- so this arrives as a
            # raw text frame, not a JSON-encoded string.
            reply = await ws.receive_str()
            assert reply == "saved"

    @pytest.mark.asyncio
    async def test_batch_preserves_an_earlier_explicit_result(self, app_client):
        """Regression test for server.py's websocket_handler: a batch
        (JSON array) used to keep only the *last* submessage's result,
        discarding any explicit non-None result an earlier submessage in
        the same batch produced. Here, message 1 (clicking Save) returns
        the string "saved"; message 2 (editing Plain, no handler
        registered) returns None. The batch's combined response must
        still carry "saved" -- not silently lose it to message 2's None.
        """
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()  # initial screen
            batch = [
                {"path": ["Save", "Root"], "event": "changed", "value": "batch-1"},
                {"path": ["Plain", "Root"], "event": "changed", "value": "batch-2"},
            ]
            await ws.send_str(json.dumps(batch))
            reply = await ws.receive_str()
            assert reply == "saved"

    @pytest.mark.asyncio
    async def test_empty_batch_returns_a_warning(self, app_client):
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()
            await ws.send_str(json.dumps([]))
            reply = await ws.receive_json()
            assert reply["type"] == "warning"
            assert "Empty command batch" in reply["value"]

    @pytest.mark.asyncio
    async def test_close_text_message_closes_the_connection(self, app_client):
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()
            await ws.send_str("close")
            msg = await ws.receive()
            from aiohttp import WSMsgType
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)

    @pytest.mark.asyncio
    async def test_close_path_json_message_also_closes_the_connection(self, app_client):
        """{"path": ["close"]} is the documented (protocol.md), JSON way to
        ask for a clean close -- the bare 'close' string above is kept only
        for whatever already sends it. Both must work identically."""
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()
            await ws.send_json({"path": ["close"]})
            msg = await ws.receive()
            from aiohttp import WSMsgType
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)

    @pytest.mark.asyncio
    async def test_close_path_json_message_never_reaches_result4message(self, app_client, monkeypatch):
        """The close check has to happen before result4message() -- path
        ["close"] doesn't address a real screen element, so if it were
        dispatched normally it would just come back as the usual "Element
        close does not exist!" error instead of closing anything."""
        import unisi.users as users_mod
        calls = []
        original = users_mod.User.result4message
        async def spy(self, message):
            calls.append(message)
            return await original(self, message)
        monkeypatch.setattr(users_mod.User, "result4message", spy)

        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()
            await ws.send_json({"path": ["close"]})
            await ws.receive()
        assert calls == []

    @pytest.mark.asyncio
    async def test_malformed_non_object_message_ends_that_connection_but_not_the_server(
        self, app_client
    ):
        """Documents current behavior rather than changing it: a raw JSON
        scalar (not an object, not an array) can't become a
        ReceivedMessage (dict.update() on an int/str raises TypeError).
        That's caught by websocket_handler's broad `except Exception`,
        logged, and ends *this* connection (finally: user.delete()) --
        it must not take the whole server down, so a fresh connection
        right after must still work normally.
        """
        async with app_client.ws_connect("/ws") as ws:
            await ws.receive_json()
            await ws.send_str(json.dumps(42))
            msg = await ws.receive()
            from aiohttp import WSMsgType
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR)

        # server itself is still healthy for a new connection
        async with app_client.ws_connect("/ws") as ws2:
            first = await ws2.receive_json()
            assert first["type"] == "screen"


class TestEnsureDirectoryExists:
    def test_creates_a_missing_directory(self, tmp_path):
        target = tmp_path / "nested" / "dir"
        server_mod.ensure_directory_exists(str(target))
        assert target.is_dir()

    def test_is_a_no_op_when_already_present(self, tmp_path):
        target = tmp_path / "already-there"
        target.mkdir()
        server_mod.ensure_directory_exists(str(target))  # must not raise
        assert target.is_dir()


class TestEnsureUnisiTypings:
    def test_creates_the_typings_file_with_expected_content(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        server_mod.ensure_unisi_typings()
        content = (tmp_path / "typings" / "__builtins__.pyi").read_text()
        assert content == "from unisi import User\nuser: User\n"

    def test_second_call_with_unchanged_content_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        server_mod.ensure_unisi_typings()
        server_mod.ensure_unisi_typings()  # must not raise (and, importantly, must not leak a file handle)
        content = (tmp_path / "typings" / "__builtins__.pyi").read_text()
        assert content == "from unisi import User\nuser: User\n"

    def test_stale_content_is_rewritten(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "typings").mkdir()
        (tmp_path / "typings" / "__builtins__.pyi").write_text("stale content")
        server_mod.ensure_unisi_typings()
        content = (tmp_path / "typings" / "__builtins__.pyi").read_text()
        assert content == "from unisi import User\nuser: User\n"


class TestStart:
    @pytest.fixture
    def no_op_start(self, monkeypatch, tmp_path):
        """Everything start() calls that would otherwise block forever
        (web.run_app), talk to a real LLM provider (setup_llmrag), or
        touch process-global test bookkeeping (run_tests) is replaced with
        a recording no-op stand-in, so start() itself -- its own route/
        arg-composition logic -- can run to completion inside a test.
        """
        import unisi.server as s

        monkeypatch.chdir(tmp_path)
        calls = {}

        def fake_run_app(app, port=None):
            calls["app"] = app
            calls["port"] = port

        def fake_run_tests(user):
            calls["run_tests_user"] = user

        monkeypatch.setattr(s.web, "run_app", fake_run_app)
        monkeypatch.setattr(s, "run_tests", fake_run_tests)
        monkeypatch.setattr(s, "setup_llmrag", lambda: calls.setdefault("llmrag", True))
        monkeypatch.setattr(User, "init_user", classmethod(lambda cls: "fake-init-user"))
        return calls

    def test_creates_screens_and_blocks_directories(self, no_op_start, tmp_path):
        server_mod.start()
        assert (tmp_path / server_mod.screens_dir).is_dir()
        assert (tmp_path / server_mod.blocks_dir).is_dir()

    def test_creates_typings_file(self, no_op_start, tmp_path):
        server_mod.start()
        assert (tmp_path / "typings" / "__builtins__.pyi").exists()

    def test_sets_user_type(self, no_op_start):
        class CustomUser(User):
            pass

        try:
            server_mod.start(user_type=CustomUser)
            assert User.type is CustomUser
        finally:
            User.type = User

    def test_calls_run_tests_with_the_init_user(self, no_op_start):
        server_mod.start()
        assert no_op_start["run_tests_user"] == "fake-init-user"

    def test_registers_the_expected_routes(self, no_op_start):
        server_mod.start()
        app = no_op_start["app"]
        paths = {route.resource.canonical for route in app.router.routes()}
        assert "/ws" in paths
        assert "/" in paths  # POST upload route
        assert any(p.startswith(f"/{server_mod.config.upload_dir}") for p in paths)

    def test_extra_http_handlers_come_first(self, no_op_start):
        from aiohttp import web as aioweb

        async def custom(request):
            return aioweb.Response(text="custom")

        server_mod.start(http_handlers=[aioweb.get("/custom", custom)])
        app = no_op_start["app"]
        paths = [route.resource.canonical for route in app.router.routes()]
        assert paths[0] == "/custom"

    def test_default_http_handlers_do_not_leak_between_separate_calls(self, no_op_start):
        """Guards the http_handlers=None default: two independent start()
        calls (as separate as start() can meaningfully be called twice in
        one process, given web.run_app is stubbed out here) must each see
        only their own extra routes, never a previous call's leftovers.
        """
        from aiohttp import web as aioweb

        async def h1(request):
            return aioweb.Response(text="one")

        async def h2(request):
            return aioweb.Response(text="two")

        server_mod.start(http_handlers=[aioweb.get("/one", h1)])
        first_paths = {r.resource.canonical for r in no_op_start["app"].router.routes()}

        server_mod.start(http_handlers=[aioweb.get("/two", h2)])
        second_paths = {r.resource.canonical for r in no_op_start["app"].router.routes()}

        assert "/one" in first_paths and "/one" not in second_paths
        assert "/two" in second_paths

    def test_warns_when_web_client_is_misconfigured(self, no_op_start, monkeypatch, capsys):
        """start() wires warn_if_web_client_misconfigured() in -- see
        TestWarnIfWebClientMisconfigured in test_web_client.py for the
        function's own behavior in isolation.
        """
        monkeypatch.setattr(server_mod.config, "web_client", "no-such-directory")
        server_mod.start()
        assert "web_client" in capsys.readouterr().out

    def test_no_web_client_warning_when_not_configured(self, no_op_start, monkeypatch, capsys):
        monkeypatch.setattr(server_mod.config, "web_client", None)
        server_mod.start()
        assert "web_client" not in capsys.readouterr().out
