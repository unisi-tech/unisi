"""
Tests for the administrator's panel: unisi/admin.py, and its hooks in server.py
(`make_user`, `websocket_handler`, `static_serve`), users.py (`User.process`) and
common.py (`ReceivedMessage.admin_type`).

The panel is made of UNISI units and talks the ordinary websocket protocol, so most of
it is tested the way a web client uses it -- over a real websocket of `app_client`
(conftest.py): `/ws?page=admin` is the page /admin, `{"path": ["admin"]}` is the button of the
toolbar, a click on a row is `changed` of the table, and so on. The password and the
list are also checked directly, on a user wired without a socket (adminkit.py).
"""
import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

import unisi.admin as admin_mod
import unisi.server as server_mod
from adminkit import connect, ids, make_folder, row, save_state, sid, wire
from unisi.admin import (ADMIN_PAGE, AdminDialog, AdminUser, admin_enabled, admin_password, is_admin_message,
                         open_panel, password_ok, panel)
from unisi.common import ReceivedMessage, Unishare
from unisi.persist import Persist
from unisi.sessions import session_registry
from unisi.users import User

pytestmark = pytest.mark.usefixtures("clean_state")

TABLE, PANEL = "Sessions", "Administration"
BUTTON_ADMIN = {"path": ["admin"], "event": "changed", "value": None}


# ── a web client ──────────────────────────────────────────────────────────────
class Client:
    """what the web client does with the connection: sends messages, reads the answers"""

    def __init__(self, ws):
        self.ws = ws

    async def send(self, path, event="changed", value=None):
        await self.ws.send_str(json.dumps({"path": path, "event": event, "value": value}))

    async def next(self, type=None, timeout=3):
        """the next message (of `type`); `null`, which the server answers a handler that
        returns nothing with, is skipped"""
        while True:
            msg = await asyncio.wait_for(self.ws.receive(), timeout)
            if msg.type.name != "TEXT":
                return None if type is None else pytest.fail(f"the connection is closed, no {type}")
            data = json.loads(msg.data) if msg.data.startswith(("{", "[", "null")) else msg.data
            if data is None:
                continue
            if type is None or isinstance(data, dict) and data.get("type") == type:
                return data

    async def open_panel(self):
        await self.send(["admin"])
        return await self.next("dialog")

    async def click(self, index):
        await self.send([TABLE, PANEL], "changed", index)

    async def command(self, name):
        await self.send([name, PANEL])

    async def closed(self, timeout=3):
        while True:
            msg = await asyncio.wait_for(self.ws.receive(), timeout)
            if msg.type.name in ("CLOSE", "CLOSING", "CLOSED"):
                return True


def unit(data, name):
    """the unit called `name` somewhere in the JSON of a screen or dialog"""
    if isinstance(data, dict):
        if data.get("name") == name:
            return data
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            if (found := unit(item, name)) is not None:
                return found
    return None


def table_of(dialog):
    return next(unit for unit in (u for part in dialog["value"] for u in (part if isinstance(part, list) else [part]))
                if unit.get("type") == "table")


def listed(dialog):
    """{session: last used or 'active'} of the table of a panel"""
    return {r[0]: r[2] for r in table_of(dialog)["rows"]}


async def settle(condition, tries=100):
    for _ in range(tries):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return condition()


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(admin_mod, "WRONG_PASSWORD_DELAY", 0)


@pytest_asyncio.fixture
async def connection(app_client):
    """a client connected to a session of its own, the first screen already received"""
    s = sid("main")
    async with app_client.ws_connect(f"/ws?session={s}") as ws:
        client = Client(ws)
        client.session = s
        assert (await client.next("screen"))["name"] == "Home"
        yield client


# ── the password ──────────────────────────────────────────────────────────────
class TestPassword:
    @pytest.mark.parametrize("value", [None, ""])
    def test_not_set_means_no_password(self, monkeypatch, value):
        monkeypatch.setattr(server_mod.config, "admin", value)
        assert admin_password() is None and admin_enabled()
        assert password_ok(None) and password_ok("anything")

    def test_false_turns_the_panel_off_rather_than_opening_it(self, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", False)
        assert not admin_enabled()
        assert admin_password() is None

    def test_set_is_the_password(self, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        assert admin_password() == "s3cret"
        assert password_ok("s3cret")
        assert not password_ok("S3cret")
        assert not password_ok("")
        assert not password_ok(None)
        assert not password_ok(["s3cret"])

    def test_a_number_is_a_password_too(self, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", 1234)
        assert password_ok("1234") and not password_ok(1234)

    def test_missing_from_config_is_no_password(self, monkeypatch):
        monkeypatch.delattr(server_mod.config, "admin", raising=False)
        assert admin_password() is None

    def test_default_in_config(self):
        # utils.py gives every config.py a default, so apps need no `admin` at all
        import config
        assert config.admin is None


# ── the pieces ────────────────────────────────────────────────────────────────
class TestReceivedMessage:
    def test_the_message_that_opens_the_panel(self):
        assert ReceivedMessage(BUTTON_ADMIN).admin_type
        assert ReceivedMessage({"path": ["admin"], "event": "changed"}).admin_type

    @pytest.mark.parametrize("path", [["root"], ["voice"], ["close"], ["Admin"], ["admin", "Root"], ["x", "admin"], None, "admin"])
    def test_nothing_else_is(self, path):
        assert not ReceivedMessage({"path": path, "event": "changed"}).admin_type


class TestAdminUser:
    def test_serves_no_session_and_loads_nothing(self):
        before, count = User.last_user, User.count
        admin = AdminUser()
        assert admin.screens == [] and admin.screen_module is None
        assert User.last_user is before and User.count == count      # nobody's last user: mirroring is not disturbed
        assert admin.blocks == []

    def test_is_not_a_session(self):
        admin = AdminUser()
        assert admin.session not in Unishare.sessions
        assert session_registry.users(admin.session) == []

    def test_its_page_tells_the_client_to_show_the_admin_button(self):
        screen = AdminUser().screen
        assert screen.type == "screen" and screen.admin is True and screen.blocks == [] and screen.menu == []
        assert "administration" in screen.header

    def test_saves_nothing(self):
        assert not AdminUser()._persist_enabled()

    @pytest.mark.asyncio
    async def test_leaves_no_trace_when_it_goes(self):
        await AdminUser().delete()
        assert session_registry.live == {}


class TestIsAdminMessage:
    def test_the_button_is_always_a_message_of_the_panel(self, new_user):
        assert is_admin_message(new_user(), ReceivedMessage(BUTTON_ADMIN))

    def test_an_ordinary_message_is_not(self, new_user):
        user = new_user()
        assert not is_admin_message(user, ReceivedMessage({"path": ["Save", "Root"], "event": "changed"}))
        assert not is_admin_message(user, ReceivedMessage({"path": ["root"], "event": "changed", "value": "Other"}))

    def test_what_is_sent_while_the_panel_is_open_is(self, new_user):
        user = new_user()
        user.active_dialog = panel(user)
        assert is_admin_message(user, ReceivedMessage({"path": [TABLE, PANEL], "event": "changed", "value": 0}))
        assert is_admin_message(user, ReceivedMessage({"path": [PANEL], "event": "changed", "value": "Close"}))
        assert not is_admin_message(user, ReceivedMessage({"path": ["Save", "Root"], "event": "changed"}))
        assert not is_admin_message(user, ReceivedMessage({"event": "changed"}))

    def test_a_dialog_of_the_app_is_not_the_panel(self, new_user):
        from unisi.containers import Dialog
        user = new_user()
        user.active_dialog = Dialog("Sure?", lambda *_: None)
        assert not is_admin_message(user, ReceivedMessage({"path": ["Sure?"], "event": "changed", "value": "Ok"}))


class TestPanelDialog:
    def test_is_a_dialog_with_the_table_of_sessions(self, new_user):
        s = sid("listed")
        save_state(s)
        user = new_user()
        dialog = panel(user)
        assert isinstance(dialog, AdminDialog) and dialog.name == PANEL
        assert dialog.commands == ["Close", "New", "Refresh"]
        table = next(u for u in dialog.value[1:] if getattr(u, "type", None) == "table")
        assert table.name == TABLE and table.headers == ["Session", "Created", "Last used"]
        assert s in [r[0] for r in table.rows]

    def test_a_session_is_a_row_with_its_dates(self, monkeypatch):
        s = sid("dates")
        session_registry.saved[s] = {"created": 1_700_000_000, "used": 1_700_003_600}
        dialog = panel(AdminUser())
        rows = {r[0]: r for r in dialog.value[1].rows}
        assert rows[s][1] and rows[s][2] and rows[s][1] != rows[s][2]
        assert len(rows[s][1]) == len("2023-11-14 22:13")

    def test_active_is_shown_instead_of_the_last_use(self, new_user):
        s = sid("active")
        session_registry.opened(s, new_user(session=s))
        assert {r[0]: r[2] for r in panel(AdminUser()).value[1].rows}[s] == "active"

    def test_dates_that_are_not_known_are_empty_cells(self):
        session_registry.saved["unused"] = {"created": 50, "used": None}
        session_registry.saved["legacy"] = {}
        rows = {r[0]: r for r in panel(AdminUser()).value[1].rows}
        assert rows["unused"][2] is None and rows["legacy"][1:] == [None, None]

    def test_the_session_of_the_user_is_the_selected_row(self, new_user):
        s = sid("mine")
        user = new_user(session=s)
        session_registry.opened(s, user)
        table = panel(user).value[1]
        assert table.rows[table.value][0] == s

    def test_nothing_is_selected_without_a_session(self):
        session_registry.saved["x"] = {"created": 1, "used": 2}
        assert panel(AdminUser()).value[1].value is None

    def test_the_table_has_the_basket_but_no_editing(self):
        table = panel(AdminUser()).value[1]
        assert table.edit is False and callable(table.delete) and not hasattr(table, "append")

    def test_serializes_for_the_client(self, new_user):
        from unisi.common import toJson
        data = json.loads(toJson(panel(new_user())))
        assert data["type"] == "dialog" and data["name"] == PANEL and data["commands"] == ["Close", "New", "Refresh"]
        table = table_of(data)
        assert table["changed"] is True and table["delete"] is True and table["edit"] is False


# ── the panel and the password, on a user without a socket ───────────────────
class TestOpenPanel:
    @pytest.mark.asyncio
    async def test_without_a_password_the_panel_opens_at_once(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", None)
        result = await open_panel(new_user())
        assert isinstance(result, AdminDialog) and result.name == PANEL

    @pytest.mark.asyncio
    async def test_with_a_password_it_is_asked_first(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        result = await open_panel(new_user())
        assert isinstance(result, AdminDialog) and result.name != PANEL
        edit = result.value[1]
        assert edit.type == "password" and edit.value == ""         # typed in a masked field
        assert result.commands == ["Enter", "Cancel"]

    @pytest.mark.asyncio
    async def test_a_turned_off_panel_is_an_error(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", False)
        result = await open_panel(new_user())
        assert result.type == "error" and "off" in result.value

    @pytest.mark.asyncio
    async def test_the_right_password_opens_the_panel_and_is_remembered(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        user = wire(new_user())
        dialog = await open_panel(user)
        dialog.value[1].value = "s3cret"
        result = await dialog.changed(dialog, "Enter")
        assert isinstance(result, AdminDialog) and result.name == PANEL
        assert user.admin_ok
        assert (await open_panel(user)).name == PANEL        # not asked again

    @pytest.mark.asyncio
    async def test_a_wrong_password_is_slow_refused_and_asked_again(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        slept = []

        async def sleep(seconds):
            slept.append(seconds)
        monkeypatch.setattr(admin_mod.asyncio, "sleep", sleep)
        user = wire(new_user())
        dialog = await open_panel(user)
        dialog.value[1].value = "s3cre"
        result = await dialog.changed(dialog, "Enter")

        assert slept == [admin_mod.WRONG_PASSWORD_DELAY] and slept[0] > 0
        assert not user.admin_ok
        assert [m.type for m in user.sent] == ["warning"] and "Wrong password" in user.sent[0].value
        assert isinstance(result, AdminDialog) and result.value[1].type == "password" and result.value[1].value == ""

    @pytest.mark.asyncio
    async def test_cancel_closes_it_and_nothing_is_granted(self, new_user, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        user = wire(new_user())
        dialog = await open_panel(user)
        dialog.value[1].value = "s3cret"            # even with the right one typed
        assert await dialog.changed(dialog, "Cancel") is None
        assert await dialog.changed(dialog, None) is None
        assert not user.admin_ok

    @pytest.mark.asyncio
    async def test_the_password_is_never_in_the_dialog_that_goes_to_the_client(self, new_user, monkeypatch):
        from unisi.common import toJson
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        assert "s3cret" not in toJson(await open_panel(new_user()))

    @pytest.mark.asyncio
    async def test_what_is_typed_does_not_stay_in_the_last_message(self, new_user, monkeypatch):
        """User.last_message is what the log of an error and the monitor show"""
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        user = wire(new_user())
        user.active_dialog = dialog = await open_panel(user)
        await user.result4message(ReceivedMessage({"path": ["Password", dialog.name], "event": "changed", "value": "s3cret"}))

        assert "s3cret" not in str(user.last_message) and "***" in str(user.last_message)
        assert dialog.value[1].value == "s3cret"        # but the field has it: that is what Enter is checked against

    @pytest.mark.asyncio
    async def test_only_a_password_field_is_masked(self, new_user):
        from unisi.containers import Dialog
        from unisi.units import Edit
        user = wire(new_user())
        user.active_dialog = dialog = Dialog("Name?", lambda *_: None, Edit("Name", ""), commands=["Ok"])
        await user.result4message(ReceivedMessage({"path": ["Name", dialog.name], "event": "changed", "value": "Ann"}))
        assert "Ann" in str(user.last_message)


# ── the page /admin ───────────────────────────────────────────────────────────
class TestAdminPage:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", [ADMIN_PAGE, ADMIN_PAGE + "/"])
    async def test_is_the_web_client_itself(self, app_client, url):
        resp = await app_client.get(url)
        assert resp.status == 200
        assert await resp.text() == await (await app_client.get("/")).text()
        assert resp.headers.get("Cache-Control") == "no-cache"

    @pytest.mark.asyncio
    async def test_a_custom_web_client_is_served_too(self, app_client, tmp_path, monkeypatch):
        (tmp_path / "index.html").write_text("<html>custom client</html>")
        monkeypatch.setattr(server_mod.config, "web_client", str(tmp_path))
        assert "custom client" in await (await app_client.get(ADMIN_PAGE)).text()

    @pytest.mark.asyncio
    async def test_other_paths_are_not_the_panel(self, app_client):
        assert (await app_client.get("/administrator")).status == 404
        assert (await app_client.get("/admin/other")).status == 404
        assert (await app_client.get("/admin/api")).status == 404      # there is no API: the panel is made of units

    @pytest.mark.asyncio
    async def test_has_no_post_handler(self, app_client):
        assert (await app_client.post("/admin/api", json={"action": "list"})).status in (404, 405)

    @pytest.mark.asyncio
    async def test_opening_the_page_does_not_start_a_session(self, app_client):
        before = len(session_registry.live)
        await app_client.get(ADMIN_PAGE)
        assert len(session_registry.live) == before


# ── the connection of /admin ──────────────────────────────────────────────────
class TestAdminConnection:
    @pytest.mark.asyncio
    async def test_shows_the_panel_at_once_when_there_is_no_password(self, app_client, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", None)
        s = sid("listed")
        save_state(s)
        async with app_client.ws_connect("/ws?page=admin") as ws:
            client = Client(ws)
            screen = await client.next("screen")
            assert screen["name"] == "Administration" and screen["admin"] is True and screen["blocks"] == []
            dialog = await client.next("dialog")
            assert dialog["name"] == PANEL
            assert listed(dialog)[s] not in (None, "active")       # a saved session: its last use is a date
            assert table_of(dialog)["value"] is None

    @pytest.mark.asyncio
    async def test_no_session_is_started_for_it(self, app_client):
        before = dict(Unishare.sessions)
        async with app_client.ws_connect("/ws?page=admin") as ws:
            client = Client(ws)
            await client.next("dialog")
            assert dict(Unishare.sessions) == before and session_registry.live == {}
            assert "Administration" not in {u.screen.name for u in Unishare.sessions.values()}

    @pytest.mark.asyncio
    async def test_asks_for_the_password_first_when_config_has_one(self, app_client, monkeypatch, no_delay):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        async with app_client.ws_connect("/ws?page=admin") as ws:
            client = Client(ws)
            await client.next("screen")
            ask = await client.next("dialog")
            assert ask["name"] != PANEL
            assert "s3cret" not in json.dumps(ask)
            edit = next(u for u in ask["value"][1:2] if isinstance(u, dict))
            assert edit["type"] == "password"

            # a wrong one: a warning, and the same question again
            await client.send(["Password", ask["name"]], "changed", "nope")
            await client.send(["Enter", ask["name"]])
            assert (await client.next("warning"))["value"] == "Wrong password"
            again = await client.next("dialog")
            assert again["name"] == ask["name"]

            # the right one: the panel
            await client.send(["Password", again["name"]], "changed", "s3cret")
            await client.send(["Enter", again["name"]])
            panel_dialog = await client.next("dialog")
            assert panel_dialog["name"] == PANEL

    @pytest.mark.asyncio
    async def test_the_server_never_says_what_was_typed(self, app_client, monkeypatch, no_delay):
        """every frame of the whole entering of the password, key by key, wrong and right: nothing
        of what the client sent comes back (an update of the field would, were it not quiet)"""
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")

        async def said(ws, quiet=0.15):
            frames = []
            while True:
                try:
                    frames.append((await asyncio.wait_for(ws.receive(), quiet)).data)
                except asyncio.TimeoutError:
                    return frames

        async with app_client.ws_connect("/ws?page=admin") as ws:
            client = Client(ws)
            frames = await said(ws)
            ask = next(json.loads(f) for f in frames if f.startswith("{") and json.loads(f).get("type") == "dialog")
            for attempt in ("nope", "s3cret"):
                for n in range(1, len(attempt) + 1):                    # a keystroke, a message
                    await client.send(["Password", ask["name"]], "changed", attempt[:n])
                    frames += await said(ws)
                await client.send(["Enter", ask["name"]])
                frames += await said(ws)
                if attempt == "nope":
                    ask = next(json.loads(f) for f in frames[::-1] if f.startswith("{") and json.loads(f).get("type") == "dialog")
            parsed = [json.loads(f) for f in frames if f.startswith("{")]
            assert any(m.get("type") == "dialog" and m.get("name") == PANEL for m in parsed)    # it got in: all was played
            assert not [f for f in frames if "nope" in f or "s3cret" in f]

    @pytest.mark.asyncio
    async def test_the_password_dialog_can_be_cancelled(self, app_client, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        async with app_client.ws_connect("/ws?page=admin") as ws:
            client = Client(ws)
            await client.next("screen")
            ask = await client.next("dialog")
            await client.send(["Cancel", ask["name"]])
            assert (await client.next("action"))["value"] == "close"
            await client.send(["admin"])                # the button of the toolbar: asks again
            assert (await client.next("dialog"))["name"] == ask["name"]

    @pytest.mark.asyncio
    async def test_the_password_is_not_the_one_of_a_url_or_a_message_of_another_kind(self, app_client, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        async with app_client.ws_connect("/ws?page=admin&password=s3cret") as ws:
            client = Client(ws)
            await client.next("screen")
            assert (await client.next("dialog"))["name"] != PANEL

    @pytest.mark.asyncio
    async def test_a_turned_off_panel_leaves_the_url_to_the_app(self, app_client, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", False)
        async with app_client.ws_connect("/ws?page=admin") as ws:
            client = Client(ws)
            assert (await client.next("screen"))["name"] == "Home"      # an ordinary session
            await client.send(["admin"])
            error = await client.next("error")
            assert "off" in error["value"]


# ── the button of the toolbar ─────────────────────────────────────────────────
class TestAdminButton:
    @pytest.mark.asyncio
    async def test_opens_the_panel_in_any_session(self, connection, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", None)
        dialog = await connection.open_panel()
        assert dialog["name"] == PANEL
        assert listed(dialog)[connection.session] == "active"
        table = table_of(dialog)
        assert table["rows"][table["value"]][0] == connection.session      # the session it is pressed in is selected

    @pytest.mark.asyncio
    async def test_asks_for_the_password_once_per_connection(self, connection, monkeypatch, no_delay):
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        ask = await connection.open_panel()
        assert ask["name"] != PANEL
        await connection.send(["Password", ask["name"]], "changed", "s3cret")
        await connection.send(["Enter", ask["name"]])
        assert (await connection.next("dialog"))["name"] == PANEL
        await connection.command("Close")
        assert (await connection.open_panel())["name"] == PANEL            # not asked again

    @pytest.mark.asyncio
    async def test_the_session_goes_on_as_before(self, connection):
        await connection.open_panel()
        await connection.command("Close")
        assert (await connection.next("action"))["value"] == "close"
        await connection.send(["Save", "Root"], "changed", "x")
        assert await connection.next() == "saved"
        assert Unishare.sessions[connection.session].screen.name == "Home"

    @pytest.mark.asyncio
    async def test_close_just_closes(self, connection):
        await connection.open_panel()
        await connection.command("Close")
        assert (await connection.next("action"))["value"] == "close"
        assert Unishare.sessions[connection.session].active_dialog is None

    @pytest.mark.asyncio
    async def test_refresh_reads_the_list_again(self, connection):
        dialog = await connection.open_panel()
        s = sid("late")
        save_state(s)
        assert s not in listed(dialog)
        await connection.command("Refresh")
        assert s in listed(await connection.next("dialog"))


# ── what the panel does ───────────────────────────────────────────────────────
def index_of(dialog, session):
    return [r[0] for r in table_of(dialog)["rows"]].index(session)


class TestLoading:
    @pytest.mark.asyncio
    async def test_a_click_loads_a_saved_session_into_the_connection(self, connection):
        target = sid("saved")
        save_state(target)
        dialog = await connection.open_panel()
        await connection.click(index_of(dialog, target))

        assert (await connection.next("action"))["value"] == "close"        # the panel goes away
        screen = await connection.next("screen")
        assert screen["name"] == "Home"
        # the connection is the session now ...
        assert Unishare.sessions[target].session == target
        assert target in session_registry.live and listed(await connection.open_panel())[target] == "active"
        # ... and the one it left is a session of the past (an anonymous one leaves nothing behind)
        assert connection.session not in Unishare.sessions
        assert row(connection.session) is None

    @pytest.mark.asyncio
    async def test_the_session_left_is_remembered_with_its_last_use(self, connection):
        save_state(connection.session)
        target = sid("target")
        save_state(target)
        dialog = await connection.open_panel()
        await connection.click(index_of(dialog, target))
        await connection.next("screen")
        gone = row(connection.session)
        assert gone["active"] is False and gone["used"]

    @pytest.mark.asyncio
    async def test_the_saved_state_of_a_session_is_what_the_connection_shows(self, app_client, connection, monkeypatch):
        monkeypatch.setattr(server_mod.config, "persist", True, raising=False)       # whatever the screens hold is saved
        target = sid("state")
        async with app_client.ws_connect(f"/ws?session={target}") as ws:
            worker = Client(ws)
            await worker.next("screen")
            await worker.send(["Plain", "Root"], "changed", "typed by the user")
            assert await settle(lambda: Persist.exists(target))
        assert await settle(lambda: target not in session_registry.live)       # the client has gone

        dialog = await connection.open_panel()
        await connection.click(index_of(dialog, target))
        screen = await connection.next("screen")
        assert unit(screen, "Plain")["value"] == "typed by the user"

    @pytest.mark.asyncio
    async def test_a_live_session_of_another_client_is_joined(self, app_client, connection):
        s = sid("other-client")
        async with app_client.ws_connect(f"/ws?session={s}") as other_ws:
            other = Client(other_ws)
            await other.next("screen")
            root = Unishare.sessions[s]
            dialog = await connection.open_panel()
            assert listed(dialog)[s] == "active"
            await connection.click(index_of(dialog, s))
            await connection.next("screen")

            assert Unishare.sessions[s] is root                 # the first user stays the root of the session
            assert len(root.reflections) == 2 and root.reflections[0] is root
            assert session_registry.users(s) == root.reflections

    @pytest.mark.asyncio
    @pytest.mark.parametrize("closing", ["button", "dismissal"])
    async def test_the_panel_of_one_is_nobody_elses_business(self, app_client, connection, closing):
        """a session two clients work in: what the administrator does in the panel -- opens it,
        closes it by a button or by Esc -- is not reflected to the other client, whose first message
        after it all is the answer to its own"""
        s = sid("shared")
        async with app_client.ws_connect(f"/ws?session={s}") as other_ws:
            other = Client(other_ws)
            await other.next("screen")
            dialog = await connection.open_panel()
            await connection.click(index_of(dialog, s))             # joins it
            await connection.next("screen")
            assert len(Unishare.sessions[s].reflections) == 2

            await connection.open_panel()
            if closing == "button":
                await connection.command("Close")
            else:
                await connection.send([PANEL], "changed", None)     # what Esc sends
            await other.send(["Save", "Root"], "changed", "ping")
            assert await other.next() == "saved"

    @pytest.mark.asyncio
    async def test_a_click_on_the_session_the_connection_is_in_closes_the_panel(self, connection, monkeypatch):
        monkeypatch.setattr(server_mod.config, "admin", None)
        dialog = await connection.open_panel()
        # the client toggles the selected row off, then on again: the second `changed` is a click on it
        await connection.click(index_of(dialog, connection.session))
        assert (await connection.next("action"))["value"] == "close"
        assert connection.session in Unishare.sessions

    @pytest.mark.asyncio
    async def test_nothing_is_loaded_by_a_deselection(self, connection):
        await connection.open_panel()
        await connection.click(None)
        await connection.click([0, 1])          # the multiple mode only marks rows
        await connection.command("Close")
        assert (await connection.next("action"))["value"] == "close"
        assert connection.session in Unishare.sessions

    @pytest.mark.asyncio
    async def test_a_stale_list_does_not_load_the_wrong_session(self, connection):
        await connection.open_panel()
        await connection.click(10_000)
        assert "out of date" in (await connection.next("error"))["value"]
        assert connection.session in Unishare.sessions

    @pytest.mark.asyncio
    async def test_a_session_that_cannot_be_loaded_is_an_error_not_a_crash(self, connection, monkeypatch):
        target = sid("broken")
        save_state(target)

        def boom(*args, **kwargs):
            raise RuntimeError("corrupted state")
        monkeypatch.setattr(admin_mod, "open_session", boom)
        dialog = await connection.open_panel()
        await connection.click(index_of(dialog, target))
        assert "corrupted state" in (await connection.next("error"))["value"]
        assert connection.session in Unishare.sessions          # still where it was
        assert target not in session_registry.live

    @pytest.mark.asyncio
    async def test_what_the_left_user_still_does_does_not_reach_the_page(self, connection):
        target = sid("late-task")
        save_state(target)
        left = Unishare.sessions[connection.session]
        dialog = await connection.open_panel()
        await connection.click(index_of(dialog, target))
        await connection.next("screen")
        await left.send("progress of a task of the session left")        # a long task of it goes on
        await connection.send(["Save", "Root"], "changed", "x")
        assert await connection.next() == "saved"                         # the first thing the page gets is the answer of the new one


class TestNew:
    @pytest.mark.asyncio
    async def test_creates_a_session_and_loads_it(self, connection):
        before = set(session_registry.saved)
        await connection.open_panel()
        await connection.command("New")
        assert (await connection.next("action"))["value"] == "close"
        assert (await connection.next("screen"))["name"] == "Home"
        (created,) = set(session_registry.saved) - before
        assert Unishare.sessions[created].session == created
        assert created in session_registry.live
        assert session_registry.saved[created]["created"]           # and it will be in the list when it is left
        assert connection.session not in Unishare.sessions

    @pytest.mark.asyncio
    async def test_the_new_session_is_a_used_one_when_the_connection_leaves_it(self, connection):
        await connection.open_panel()
        await connection.command("New")
        await connection.next("screen")
        (created,) = (s for s, e in session_registry.saved.items() if e["used"] is None)
        assert row(created)["active"]

        target = sid("elsewhere")
        save_state(target)
        dialog = await connection.open_panel()
        assert listed(dialog)[created] == "active"
        await connection.click(index_of(dialog, target))
        await connection.next("screen")
        left = row(created)
        assert left["active"] is False and left["used"] and left["created"]


class TestDelete:
    @pytest.mark.asyncio
    async def test_the_basket_asks_first(self, connection):
        victim = sid("victim")
        save_state(victim)
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", index_of(dialog, victim))
        ask = await connection.next("dialog")
        assert victim in ask["name"] and ask["commands"] == ["Cancel", "Delete"]
        assert Persist.exists(victim)               # nothing is deleted yet

    @pytest.mark.asyncio
    async def test_cancel_gives_the_list_back(self, connection):
        victim = sid("spared")
        save_state(victim)
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", index_of(dialog, victim))
        ask = await connection.next("dialog")
        await connection.send(["Cancel", ask["name"]])
        back = await connection.next("dialog")
        assert back["name"] == PANEL and victim in listed(back)
        assert Persist.exists(victim)

    @pytest.mark.asyncio
    async def test_delete_removes_the_session_with_its_files_and_stays_in_the_panel(self, connection):
        victim = sid("doomed")
        save_state(victim)
        folder = make_folder(victim)
        session_registry.saved[victim] = {"created": 1, "used": 2}
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", index_of(dialog, victim))
        ask = await connection.next("dialog")
        await connection.send(["Delete", ask["name"]])
        back = await connection.next("dialog")

        assert back["name"] == PANEL and victim not in listed(back)
        assert not Persist.exists(victim) and not folder.exists()
        assert victim not in session_registry.saved
        assert connection.session in Unishare.sessions       # the connection of the administrator is as it was

    @pytest.mark.asyncio
    async def test_several_sessions_marked_in_the_multiple_mode_go_together(self, connection):
        victims = [sid("m1"), sid("m2")]
        for v in victims:
            save_state(v)
        keeper = sid("keeper")
        save_state(keeper)
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", [index_of(dialog, v) for v in victims])
        ask = await connection.next("dialog")
        assert "2 sessions" in ask["name"]
        await connection.send(["Delete", ask["name"]])
        back = await connection.next("dialog")
        assert not any(v in listed(back) for v in victims) and keeper in listed(back)
        assert not any(Persist.exists(v) for v in victims) and Persist.exists(keeper)

    @pytest.mark.asyncio
    async def test_the_session_the_administrator_is_in_can_be_deleted_too(self, connection):
        save_state(connection.session)
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", index_of(dialog, connection.session))
        ask = await connection.next("dialog")
        await connection.send(["Delete", ask["name"]])

        # what is left of the connection is the page of the panel
        assert (await connection.next("screen"))["name"] == "Administration"
        back = await connection.next("dialog")
        assert back["name"] == PANEL and connection.session not in listed(back)
        assert table_of(back)["value"] is None
        assert not Persist.exists(connection.session)
        assert connection.session not in Unishare.sessions and connection.session not in session_registry.live
        assert connection.ws.closed is False
        # the panel on that page works: a session can be loaded from it
        target = sid("after")
        save_state(target)
        await connection.send(["Refresh", PANEL])
        again = await connection.next("dialog")
        await connection.click(index_of(again, target))
        assert (await connection.next("screen"))["name"] == "Home"
        assert Unishare.sessions[target].session == target

    @pytest.mark.asyncio
    async def test_a_session_of_another_client_is_deleted_under_it(self, app_client, connection):
        s = sid("evicted")
        async with app_client.ws_connect(f"/ws?session={s}") as ws:
            other = Client(ws)
            await other.next("screen")
            save_state(s)
            dialog = await connection.open_panel()
            await connection.send([TABLE, PANEL], "delete", index_of(dialog, s))
            ask = await connection.next("dialog")
            await connection.send(["Delete", ask["name"]])
            await connection.next("dialog")

            told = await other.next("error")
            assert "deleted" in told["value"]
            assert await other.closed()
        assert await settle(lambda: s not in session_registry.live)
        assert not Persist.exists(s) and row(s) is None

    @pytest.mark.asyncio
    async def test_files_that_cannot_be_removed_are_reported(self, connection, monkeypatch):
        import unisi.sessions as sessions_mod
        victim = sid("stuck")
        save_state(victim)
        make_folder(victim)
        session_registry.saved[victim] = {"created": 1, "used": 2}

        def boom(path):
            raise PermissionError(13, "Permission denied")
        monkeypatch.setattr(sessions_mod.shutil, "rmtree", boom)
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", index_of(dialog, victim))
        ask = await connection.next("dialog")
        await connection.send(["Delete", ask["name"]])
        error = await connection.next("error")
        assert victim in error["value"] and "Permission denied" in error["value"]
        back = await connection.next("dialog")
        assert back["name"] == PANEL and victim in listed(back)

    @pytest.mark.asyncio
    async def test_a_session_that_is_gone_meanwhile_is_reported(self, connection):
        victim = sid("twice")
        save_state(victim)
        dialog = await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", index_of(dialog, victim))
        ask = await connection.next("dialog")
        Persist(victim).close()
        for suffix in ("", "-wal", "-shm"):
            Path(Persist.db_path_for(victim) + suffix).unlink(missing_ok=True)      # somebody else was quicker
        await connection.send(["Delete", ask["name"]])
        assert "Unknown session" in (await connection.next("error"))["value"]
        assert (await connection.next("dialog"))["name"] == PANEL

    @pytest.mark.asyncio
    async def test_nothing_to_delete_gives_the_list_back(self, connection):
        await connection.open_panel()
        await connection.send([TABLE, PANEL], "delete", None)
        assert (await connection.next("dialog"))["name"] == PANEL
        await connection.send([TABLE, PANEL], "delete", 10_000)
        assert (await connection.next("dialog"))["name"] == PANEL


# ── secrets stay out of the records of the server ─────────────────────────────
class TestRecorder:
    @pytest.mark.asyncio
    async def test_the_messages_of_the_panel_are_not_recorded_for_autotest(self, connection, monkeypatch, no_delay):
        recorded = []
        monkeypatch.setattr(server_mod.recorder, "record_file", "somewhere")
        monkeypatch.setattr(server_mod.recorder, "accept", lambda message, prepared: recorded.append(str(message)))
        monkeypatch.setattr(server_mod.config, "admin", "s3cret")
        ask = await connection.open_panel()
        await connection.send(["Password", ask["name"]], "changed", "s3cret")
        await connection.send(["Enter", ask["name"]])
        await connection.next("dialog")
        await connection.command("Close")
        await connection.next("action")
        await connection.send(["Save", "Root"], "changed", "x")
        await connection.next()
        assert recorded == [str(ReceivedMessage({"path": ["Save", "Root"], "event": "changed", "value": "x"}))]
        assert "s3cret" not in " ".join(recorded)
