"""
Tests for the session registry and the deletion of sessions: unisi/sessions.py, and
its hooks in server.py (`make_user`) and users.py (`User.delete`).

Layered like the rest of tests/core:
  * valid_id / file_times are small pure functions -- tested directly.
  * SessionRegistry and delete_session work on real files (`users/<id>.db`,
    `<upload_dir>/<id>`, `users/sessions.json`) of the fixtures_app working
    dir, with real Users where a session has to be "connected" -- the same
    "real objects over mocks" philosophy as the rest of the suite. The only
    doubles are the websocket (`FakeWs`) and the Users' `send`, which in
    production are closures over a real socket.
"""
import json
import os
import sqlite3
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

import unisi.server as server_mod
import unisi.sessions as sessions_mod
from adminkit import connect, ids, make_folder, row, save_state, sid, wire
from unisi.common import Message, Unishare
from unisi.persist import Persist
from unisi.sessions import (SessionError, SessionRegistry, delete_session, file_times, open_session,
                            session_registry, valid_id)
from unisi.users import User

pytestmark = pytest.mark.usefixtures("clean_state")


class Clock:
    now = 0.0

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """the clock sessions.py reads -- and nobody else: patching time.time itself would be
    consumed by every thread of the test process"""
    monkeypatch.setattr(sessions_mod, "time", Clock())
    return sessions_mod.time


# ── pure helpers ──────────────────────────────────────────────────────────────
class TestValidId:
    @pytest.mark.parametrize("session", ["aB3dE5gH7j-12", "pytest_1_2", "explicit-session-1", "a.b", "x", "Ünï-1", "a" * 200])
    def test_ids_that_are_a_single_file_name(self, session):
        assert valid_id(session)

    @pytest.mark.parametrize("session", ["", ".", "..", "../x", "a/b", "a\\b", "/etc", "x\n", "x\0y", "a" * 201, None, 5, ["a"]])
    def test_ids_that_could_name_anything_else(self, session):
        assert not valid_id(session)


class TestFileTimes:
    def test_used_is_the_last_write_of_the_database_and_its_wal(self, tmp_path):
        db = tmp_path / "a.db"
        db.write_text("x")
        os.utime(db, (100, 100))
        (tmp_path / "a.db-wal").write_text("y")
        os.utime(tmp_path / "a.db-wal", (500, 500))
        created, used = file_times(str(db))
        assert used == 500
        assert created is None or created <= 100      # None where the platform has no creation time

    def test_missing_file(self, tmp_path):
        assert file_times(str(tmp_path / "none.db")) == (None, None)


# ── the registry ──────────────────────────────────────────────────────────────
class TestRegistry:
    def test_a_connected_session_is_active_and_has_a_creation_date(self, new_user):
        s = sid("live")
        user = new_user(session=s)
        session_registry.opened(s, user)
        r = row(s)
        assert r["active"] and r["created"] and r["used"] is None

    def test_a_session_that_leaves_nothing_behind_is_gone_with_its_connection(self, new_user):
        s = sid("ephemeral")
        user = new_user(session=s)
        session_registry.opened(s, user)
        session_registry.closed(s, user)
        assert row(s) is None
        assert not Path("users", "sessions.json").exists()

    @pytest.mark.parametrize("footprint", ["db", "folder"])
    def test_a_session_with_a_footprint_is_remembered_with_its_dates(self, new_user, clock, footprint):
        s = sid("kept")
        user = new_user(session=s)
        clock.now = 1000.0
        session_registry.opened(s, user)
        if footprint == "db":
            save_state(s)
        else:
            make_folder(s)
        clock.now = 2000.0
        session_registry.closed(s, user)

        r = row(s)
        assert (r["created"], r["used"], r["active"]) == (1000.0, 2000.0, False)
        # ... and it is in the file, which a restarted server reads back
        assert json.loads(Path("users", "sessions.json").read_text())[s] == {"created": 1000.0, "used": 2000.0}
        fresh = SessionRegistry()
        assert fresh.saved[s] == {"created": 1000.0, "used": 2000.0}

    def test_creation_date_survives_further_connections(self, new_user, clock):
        clock.now = 10.0
        s = session_registry.create()
        first, second = new_user(session=s), new_user(session=s)
        clock.now = 20.0
        session_registry.opened(s, first)
        clock.now = 30.0
        session_registry.closed(s, first)
        assert session_registry.saved[s] == {"created": 10.0, "used": 30.0}
        clock.now = 40.0
        session_registry.opened(s, second)
        clock.now = 50.0
        session_registry.closed(s, second)
        assert session_registry.saved[s] == {"created": 10.0, "used": 50.0}
        assert row(s)["used"] == 50.0

    def test_a_session_saved_before_the_registry_has_no_known_creation_date_where_the_platform_keeps_none(self, new_user, clock):
        s = sid("old-db")
        save_state(s)
        clock.now = 60.0
        user = new_user(session=s)
        session_registry.opened(s, user)
        session_registry.closed(s, user)
        created, used = session_registry.saved[s]["created"], session_registry.saved[s]["used"]
        assert used == 60.0
        assert created == file_times(Persist.db_path_for(s))[0]       # the file's, or None -- never "now"

    def test_the_session_ends_with_its_last_client(self, new_user):
        s = sid("two")
        save_state(s)
        a, b = new_user(session=s), new_user(session=s)
        session_registry.opened(s, a)
        session_registry.opened(s, b)
        session_registry.closed(s, a)
        assert row(s)["active"] and session_registry.users(s) == [b]
        assert s not in session_registry.saved
        session_registry.closed(s, b)
        assert not row(s)["active"] and s in session_registry.saved

    def test_closing_a_forgotten_session_does_not_bring_it_back(self, new_user):
        s = sid("forgotten")
        user = new_user(session=s)
        session_registry.opened(s, user)
        session_registry.forget(s)
        make_folder(s)
        session_registry.closed(s, user)
        assert s not in session_registry.saved

    def test_a_session_saved_before_the_registry_existed_is_listed_from_its_file(self):
        s = sid("legacy")
        save_state(s)
        os.utime(Persist.db_path_for(s), (777, 777))
        for suffix in ("-wal", "-shm"):
            Path(Persist.db_path_for(s) + suffix).unlink(missing_ok=True)
        r = row(s)
        assert r["used"] == 777 and not r["active"]
        assert r["created"] is None or r["created"] <= 777

    def test_files_that_are_not_sessions_are_not_listed(self):
        users = Path("users")
        users.mkdir(exist_ok=True)
        (users / "sessions.json").write_text("{}")
        (users / "notes.txt").write_text("x")
        (users / "a b\n.db").write_text("x")      # not a printable id
        assert ids(session_registry.rows()) == []

    def test_create_makes_a_saved_unused_session(self):
        s = session_registry.create()
        assert valid_id(s)
        r = row(s)
        assert r["created"] and r["used"] is None and not r["active"]
        assert SessionRegistry().saved[s]["used"] is None

    def test_created_ids_are_unique(self):
        made = {session_registry.create() for _ in range(50)}
        assert len(made) == 50

    def test_a_created_session_stays_while_unused_and_nothing_is_saved_for_it(self, new_user):
        s = session_registry.create()
        user = new_user(session=s)
        session_registry.opened(s, user)
        assert row(s)["active"]
        session_registry.closed(s, user)
        assert row(s)["used"] is not None and not row(s)["active"]

    def test_forget(self):
        s = session_registry.create()
        session_registry.forget(s)
        assert row(s) is None
        assert s not in json.loads(Path("users", "sessions.json").read_text())

    def test_order_is_active_first_then_the_most_recent(self, new_user):
        reg = session_registry
        reg.saved.update({
            "old": {"created": 1, "used": 5}, "recent": {"created": 2, "used": 50},
            "never": {"created": 40, "used": None}, "live-old": {"created": 3, "used": 90},
            "live-new": {"created": 30, "used": None}})
        for s in ("live-old", "live-new"):
            reg.opened(s, new_user(session=s))
        reg.live["live-old"]["created"], reg.live["live-new"]["created"] = 3, 30
        assert ids(reg.rows()) == ["live-new", "live-old", "recent", "never", "old"]

    def test_active_sessions_the_registry_never_saw_are_listed_too(self, new_user):
        s = sid("manual")
        Unishare.sessions[s] = new_user(session=s)
        assert row(s)["active"]

    def test_unreadable_registry_file_is_ignored(self, caplog):
        Path("users").mkdir(exist_ok=True)
        Path("users", "sessions.json").write_text("{not json")
        assert SessionRegistry().saved == {}
        assert "unreadable" in caplog.text
        Path("users", "sessions.json").write_text("[1, 2]")
        assert SessionRegistry().saved == {}
        Path("users", "sessions.json").write_text('{"a": {"created": 1}, "b": 5}')
        assert SessionRegistry().saved == {"a": {"created": 1}}

    def test_garbage_in_the_file_does_not_break_the_list(self):
        Path("users").mkdir(exist_ok=True)
        Path("users", "sessions.json").write_text('{"a": {"created": "yesterday", "used": true}}')
        assert row("a") == {"id": "a", "created": None, "used": None, "active": False}

    def test_unwritable_registry_is_only_logged(self, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise PermissionError("denied")
        monkeypatch.setattr(sessions_mod.os, "replace", boom)
        s = session_registry.create()       # does not raise
        assert row(s) and "not saved" in caplog.text


# ── deleting a session ────────────────────────────────────────────────────────
class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_removes_everything_the_session_has(self):
        s = sid("all")
        save_state(s)
        base = Persist.db_path_for(s)
        for suffix in ("-wal", "-shm"):
            Path(base + suffix).touch()
        folder = make_folder(s)
        (folder / "sub").mkdir()
        (folder / "sub" / "deep.txt").write_text("x")
        session_registry.saved[s] = {"created": 1, "used": 2}

        await delete_session(s)

        assert not Path(base).exists() and not Path(base + "-wal").exists() and not Path(base + "-shm").exists()
        assert not folder.exists()
        assert row(s) is None
        assert s not in json.loads(Path("users", "sessions.json").read_text())

    @pytest.mark.asyncio
    async def test_a_session_created_in_the_panel_without_any_files(self):
        s = session_registry.create()
        await delete_session(s)
        assert row(s) is None

    @pytest.mark.asyncio
    async def test_nothing_else_is_touched(self):
        s, other = sid("mine"), sid("other")
        for x in (s, other):
            save_state(x)
            make_folder(x)
        sibling = Path("downloads", s + "-sibling")
        sibling.mkdir()
        await delete_session(s)
        assert Persist.exists(other) and Path(sessions_mod.session_dir(other)).is_dir()
        assert sibling.is_dir()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", ".", "..", "../users", "a/b", "downloads/..", None, 7])
    async def test_an_id_that_names_something_else_is_refused(self, bad):
        with pytest.raises(SessionError, match="Invalid"):
            await delete_session(bad)

    @pytest.mark.asyncio
    async def test_unknown_session_is_refused_and_its_folder_is_left_alone(self):
        # a folder of upload_dir that is not a session's must survive a delete of its name
        s = sid("stranger")
        folder = make_folder(s)
        with pytest.raises(SessionError, match="Unknown"):
            await delete_session(s)
        assert folder.is_dir()

    @pytest.mark.asyncio
    async def test_the_autotest_session_is_not_a_session(self):
        with pytest.raises(SessionError):
            await delete_session("autotest")

    @pytest.mark.asyncio
    async def test_a_link_in_place_of_the_folder_is_removed_not_followed(self, tmp_path):
        s = sid("link")
        save_state(s)
        target = tmp_path / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        link = Path(sessions_mod.session_dir(s))
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("no symlinks here")
        await delete_session(s)
        assert not link.exists() and not link.is_symlink()
        assert (target / "keep.txt").read_text() == "keep"

    @pytest.mark.asyncio
    async def test_files_that_cannot_be_removed_keep_the_session_in_the_list(self, monkeypatch):
        s = sid("stuck")
        save_state(s)
        make_folder(s)
        session_registry.saved[s] = {"created": 1, "used": 2}

        def boom(path):
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr(sessions_mod.shutil, "rmtree", boom)

        with pytest.raises(SessionError, match="Permission denied"):
            await delete_session(s)
        assert row(s) is not None

    @pytest.mark.asyncio
    async def test_a_connected_session_is_disconnected_first(self):
        s = sid("connected")
        save_state(s)
        user = wire(connect(s))
        user.db = Persist(s)       # it has the database open
        conn = user.db.conn

        await delete_session(s)

        assert user.ws.closed
        assert [m.type for m in user.sent] == ["error"] and "deleted" in user.sent[0].value
        assert user.db is None
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")        # closed, or the file could not be removed on Windows
        assert s not in Unishare.sessions
        assert not Persist.exists(s)
        assert row(s) is None

    @pytest.mark.asyncio
    async def test_the_deleted_user_can_not_bring_the_session_back(self):
        s = sid("zombie")
        user = wire(connect(s))
        await delete_session(s)

        assert not user._persist_enabled()
        assert user._persist_db(create=True) is None
        user.set_key("k", "v")          # a handler that is still running saves something ...
        assert not Persist.exists(s)    # ... to nowhere

        await user.delete()             # the websocket handler's `finally`
        assert row(s) is None
        assert s not in session_registry.saved

    @pytest.mark.asyncio
    async def test_every_client_of_the_session_is_disconnected(self, monkeypatch):
        s = sid("many")
        # not shared: the same id opened twice -- the older user is no longer in Unishare.sessions
        monkeypatch.setattr(server_mod.config, "share", False)
        old, new = wire(connect(s)), wire(connect(s))
        assert Unishare.sessions[s] is new
        await delete_session(s)
        assert old.ws.closed and new.ws.closed
        assert not old._persist_enabled() and not new._persist_enabled()

    @pytest.mark.asyncio
    async def test_a_shared_session_with_reflections(self, monkeypatch):
        s = sid("shared")
        monkeypatch.setattr(server_mod.config, "share", False)
        root = wire(connect(s))
        monkeypatch.setattr(server_mod.config, "share", True)
        view = wire(connect(s))
        assert root.reflections == [root, view]
        await delete_session(s)
        assert root.ws.closed and view.ws.closed
        assert s not in Unishare.sessions

    @pytest.mark.asyncio
    async def test_a_client_that_does_not_go_away_does_not_hang_the_panel(self, monkeypatch):
        import asyncio
        s = sid("stubborn")
        user = wire(connect(s))

        async def never(*args):
            await asyncio.sleep(60)
        user.ws.close = never
        monkeypatch.setattr(sessions_mod, "DISCONNECT_TIMEOUT", 0.05)
        await delete_session(s)
        assert row(s) is None


    @pytest.mark.asyncio
    async def test_a_user_to_keep_loses_the_session_but_not_his_connection(self, monkeypatch):
        s = sid("keep")
        save_state(s)
        monkeypatch.setattr(server_mod.config, "share", False)
        admin, other = wire(connect(s)), wire(connect(s))
        await delete_session(s, keep=[admin])
        assert not admin.ws.closed and admin.sent == []
        assert other.ws.closed
        assert not admin._persist_enabled()         # it is only left to be moved elsewhere
        assert not Persist.exists(s) and row(s) is None


class TestOpenSession:
    def test_the_first_user_of_a_session_is_its_root_and_is_tracked(self):
        s = sid("first")
        count = User.count
        user = open_session(s)
        assert user.session == s and User.count == count + 1
        assert Unishare.sessions[s] is user
        assert session_registry.users(s) == [user]

    def test_a_view_of_a_live_session_leaves_the_root_where_it_is(self):
        s = sid("view")
        root = open_session(s)
        view = open_session(s, root, register=False)
        assert Unishare.sessions[s] is root
        assert root.reflections == [root, view]
        assert session_registry.users(s) == [root, view]

    def test_a_session_with_a_saved_state_is_resumed(self):
        s = sid("resumed")
        save_state(s)
        user = open_session(s)
        assert row(s)["active"] and Unishare.sessions[s] is user

    def test_the_screen_is_the_one_asked_for(self):
        user = open_session(sid("screen"), screen="other")
        assert user.screen.name == "Other"


# ── the hooks in User / make_user ─────────────────────────────────────────────
class TestSessionTracking:
    @pytest.mark.asyncio
    async def test_a_connection_is_tracked_from_make_user_to_user_delete(self):
        s = sid("track")
        user = connect(s)
        assert row(s)["active"]
        save_state(s)                   # the app saved something while it was connected
        await user.delete()
        r = row(s)
        assert r["active"] is False and r["used"] and r["created"]
        assert s not in Unishare.sessions

    @pytest.mark.asyncio
    async def test_an_anonymous_visit_leaves_no_trace(self):
        user, _ = server_mod.make_user(make_mocked_request("GET", "/ws"))
        assert row(user.session)["active"]
        await user.delete()
        assert row(user.session) is None

    @pytest.mark.asyncio
    async def test_a_reflection_leaving_does_not_end_the_session(self, monkeypatch):
        s = sid("reflection")
        monkeypatch.setattr(server_mod.config, "share", False)
        root = connect(s)
        monkeypatch.setattr(server_mod.config, "share", True)
        view = connect(s)
        await view.delete()
        assert row(s)["active"]
        await root.delete()
        assert row(s) is None

    @pytest.mark.parametrize("how", ["saved", "created in the panel", "state on disk"])
    def test_share_mode_resumes_a_session_nobody_is_connected_to(self, monkeypatch, how):
        s = sid("resume")
        if how == "saved":
            session_registry.saved[s] = {"created": 1, "used": 2}
        elif how == "created in the panel":
            s = session_registry.create()
        else:
            save_state(s)
        monkeypatch.setattr(server_mod.config, "share", True)
        user, status = server_mod.make_user(make_mocked_request("GET", f"/ws?session={s}"))
        assert user is not None and user.session == s
        assert Unishare.sessions[s] is user

    def test_share_mode_still_refuses_a_session_that_is_not_known(self, monkeypatch):
        monkeypatch.setattr(server_mod.config, "share", True)
        user, status = server_mod.make_user(make_mocked_request("GET", f"/ws?session={sid('nobody')}"))
        assert user is None and status.type == "error"

    def test_share_mode_still_refuses_an_id_that_is_not_a_file_name(self, monkeypatch):
        monkeypatch.setattr(server_mod.config, "share", True)
        user, status = server_mod.make_user(make_mocked_request("GET", "/ws?session=..%2Fusers"))
        assert user is None
