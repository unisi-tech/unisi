"""
The agent API (unisi/mcp.py): MCP over HTTP in a live session -- what a tool changes reaches every view of the
session at once; the general tools act as a person would; a dialog's units go ahead of it; a saved session is
resumed for the agent and a page coming to it takes its user over.
"""
import json

import pytest

from unisi.common import Message, ReceivedMessage
from unisi.containers import Dialog

pytestmark = pytest.mark.asyncio


def text(answer):
    return answer["result"]["content"][0]["text"]


def updates(sent):
    """{unit name: value} of the update messages a view got"""
    return {u["data"].name: u["data"].value for m in sent if isinstance(m, Message) for u in getattr(m, "updates", [])}


async def call(rpc, session, name, **arguments):
    status, answer = await rpc(session, "tools/call", {"name": name, "arguments": arguments})
    assert status == 200
    return answer


# ── protocol ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_initialize_ping_notifications_and_unknown_methods(rpc, client, live):
    session = live[0]
    status, answer = await rpc(session, "initialize", {"protocolVersion": "2025-03-26"})
    assert answer["result"]["protocolVersion"] == "2025-03-26"          # the client's version is echoed
    assert "tools" in answer["result"]["capabilities"]
    assert (await rpc(session, "notifications/initialized", id=None))[0] == 202
    assert (await rpc(session, "ping"))[1]["result"] == {}
    assert (await rpc(session, "server/discover"))[1]["error"]["code"] == -32601   # Claude Code asks it first
    assert (await client.get(f"/mcp?session={session}")).status == 405


async def test_tools_list_gives_general_and_app_tools_with_schemas(rpc, live):
    tools = {t["name"]: t for t in (await rpc(live[0], "tools/list"))[1]["result"]["tools"]}
    assert {"ui_screens", "ui_view", "ui_get", "ui_set", "ui_push", "ui_dialog", "code_hold",
            "note_text", "set_note", "boom"} <= set(tools)
    assert tools["note_text"]["description"] == "Текст заметки (с приставкой)."
    assert tools["note_text"]["inputSchema"] == {"type": "object", "properties": {"prefix": {"type": "string"}},
                                                 "required": []}
    assert tools["set_note"]["inputSchema"]["required"] == ["text"]
    ui_set = tools["ui_set"]["inputSchema"]
    assert set(ui_set["properties"]) == {"unit", "value", "screen"}      # the session's user is not the agent's to give
    assert ui_set["properties"]["value"] == {} and ui_set["required"] == ["unit", "value"]


async def test_unknown_session_and_unknown_tool(rpc, live):
    assert "Unknown session" in (await rpc("no-such-session", "tools/list"))[1]["error"]["message"]
    assert (await rpc(live[0], "tools/call", {"name": "nope"}))[1]["error"]["code"] == -32602


# ── tools in a live session ──────────────────────────────────────────────────────────────────────────────────────

async def test_app_tool_answers_the_agent_and_its_changes_reach_every_view(rpc, live):
    session, root, root_sent, view, view_sent = live
    answer = await call(rpc, session, "set_note", text="привет")
    assert text(answer) == "ok" and answer["result"]["isError"] is False
    assert updates(root_sent)["Note"] == "привет"           # the person's page
    assert updates(view_sent)["Note"] == "привет"           # and one more view of the session
    assert json.loads(text(await call(rpc, session, "note_text", prefix=">"))) == {"note": ">привет"}   # letters, not \\u escapes


async def test_ui_push_and_ui_set_act_as_a_person(rpc, live):
    session, root, root_sent, view, view_sent = live
    answer = json.loads(text(await call(rpc, session, "ui_push", button="Bump")))
    assert answer == {"answer": {"type": "info", "text": "bumped"}, "changed": ["Count"]}
    assert updates(root_sent)["Count"] == 1 and updates(view_sent)["Count"] == 1
    assert any(getattr(m, "type", None) == "info" for m in view_sent)         # the handler's message too
    answer = json.loads(text(await call(rpc, session, "ui_set", unit="Note", value="typed")))
    assert answer["changed"] == ["Note"] and updates(view_sent)["Note"] == "typed"


async def test_ui_view_and_ui_get(rpc, live):
    session = live[0]
    view = json.loads(text(await call(rpc, session, "ui_view")))
    main = view["blocks"][0]
    assert view["screen"] == "Agent" and main["name"] == "Main"
    assert {"name": "Rows", "type": "table", "headers": ["n"], "rows": 2, "value": None} in main["units"]
    assert json.loads(text(await call(rpc, session, "ui_get", unit="Rows")))["rows"] == [[1], [2]]
    other = json.loads(text(await call(rpc, session, "ui_view", screen="Plain")))      # without moving the page there
    assert other["blocks"][0]["units"] == [{"name": "Field", "type": "string", "value": "x"}]
    assert live[1].screen_module.screen.name == "Agent"
    screens = json.loads(text(await call(rpc, session, "ui_screens")))
    assert {"name": "Agent", "current": True} in screens and {"name": "Plain", "current": False} in screens


async def test_a_dialog_goes_after_its_units_and_ui_dialog_answers_it(rpc, live):
    session, root, root_sent, view, view_sent = live
    answer = json.loads(text(await call(rpc, session, "ui_push", button="Ask")))
    assert answer["answer"] == {"dialog": "Sure?", "commands": ["Yes", "No"]}
    kinds = [type(m).__name__ for m in root_sent]
    assert kinds.index("Message") < kinds.index("Dialog")         # the note changed before the dialog comes first
    assert updates(root_sent)["Note"] == "asked"
    answer = await call(rpc, session, "ui_dialog", command="Yes")
    assert answer["result"]["isError"] is False and root.active_dialog is None
    assert any(getattr(m, "type", None) == "action" and m.value == "close" for m in root_sent)   # the page closes it
    assert updates(root_sent)["Log"] == "Yes\n"
    assert "No dialog" in text(await call(rpc, session, "ui_dialog", command="Yes"))


async def test_a_failing_tool_is_reported_as_an_error(rpc, live):
    answer = await call(rpc, live[0], "boom")
    assert answer["result"]["isError"] is True and text(answer) == "RuntimeError: boom"


async def test_code_hold_holds_hot_reload_once(rpc, live, monkeypatch):
    import unisi.mcp as mcp
    calls = []
    monkeypatch.setattr(mcp, "hold_reload", calls.append)
    monkeypatch.setattr(mcp, "_held", False)
    for on in (True, True, False, False):
        await call(rpc, live[0], "code_hold", on=on)
    assert calls == [True, False]


# ── a session without a page ─────────────────────────────────────────────────────────────────────────────────────

async def test_a_saved_session_is_resumed_for_the_agent_and_a_page_takes_it_over(rpc, session_id):
    from aiohttp.test_utils import make_mocked_request
    from unisi.common import Unishare
    from unisi.sessions import open_session
    import unisi.server as server

    first = open_session(session_id)                    # a person worked in it and left: it is saved
    first.set_key("k", 1)
    await first.delete()
    assert session_id not in Unishare.sessions

    assert text(await call(rpc, session_id, "set_note", text="без страницы")) == "ok"
    agent_user = Unishare.sessions[session_id]
    assert agent_user.agent_only and agent_user is not first

    page_user, screens = server.make_user(make_mocked_request("GET", f"/ws?session={session_id}"))
    assert page_user is agent_user and screens                      # the page takes the agent's user over
    assert not page_user.agent_only
    assert page_user.screen_module.note.value == "без страницы"


# ── a dialog over a page's own message (users.result4message) ────────────────────────────────────────────────────

async def test_units_changed_before_a_returned_dialog_go_ahead_of_it(live):
    session, root, root_sent, view, view_sent = live
    result = await root.result4message(ReceivedMessage({"path": ["Ask", "Main"], "event": "changed", "value": None}))
    assert isinstance(result, Dialog) and root.active_dialog is result
    assert updates(root_sent) == {"Note": "asked"}                  # sent before the dialog itself


# ── a shared session: a handler's side effects reach the view that acted and the others (users.reflect) ─────────

async def receive_until(ws, pred, limit=6):
    """the messages a page gets until one satisfies pred (pushes may come before it)"""
    import asyncio
    got = []
    for _ in range(limit):
        msg = await asyncio.wait_for(ws.receive_json(), 2)
        got.append(msg)
        if pred(msg):
            return got
    raise AssertionError(f"not received: {got}")


def has_update(name, value):
    return lambda msg: any(u["data"]["name"] == name and u["data"]["value"] == value for u in msg.get("updates") or [])


@pytest.fixture
def two_pages(client, session_id, monkeypatch):
    """two pages of one session: the first one opens it, the second joins it (share)"""
    import config
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def pages():
        monkeypatch.setattr(config, "share", False)
        async with client.ws_connect(f"/ws?session={session_id}") as first:
            await first.receive_json()                                  # the screen
            monkeypatch.setattr(config, "share", True)
            async with client.ws_connect(f"/ws?session={session_id}") as second:
                await second.receive_json()
                yield first, second
    return pages


async def test_a_click_in_a_joined_page_changes_it_and_the_first_page(two_pages):
    async with two_pages() as (first, second):
        await second.send_json({"path": ["Bump", "Main"], "event": "changed", "value": None})
        await receive_until(second, has_update("Count", 1))             # its own answer has the side effect
        await receive_until(first, has_update("Count", 1))              # and the page that opened the session gets it


async def test_a_click_in_the_first_page_reaches_the_joined_one(two_pages):
    async with two_pages() as (first, second):
        await first.send_json({"path": ["Bump", "Main"], "event": "changed", "value": None})
        await receive_until(first, has_update("Count", 1))
        await receive_until(second, has_update("Count", 1))


async def test_a_dialog_opened_in_a_joined_page_comes_after_its_units_in_both(two_pages):
    async with two_pages() as (first, second):
        await second.send_json({"path": ["Ask", "Main"], "event": "changed", "value": None})
        got = await receive_until(second, lambda m: m.get("type") == "dialog")
        assert any(has_update("Note", "asked")(m) for m in got[:-1])  # the note before the dialog
        await receive_until(first, has_update("Note", "asked"))


async def test_a_page_without_session_joins_the_apps_fixed_session(monkeypatch, session_id):
    from aiohttp.test_utils import make_mocked_request
    from unisi.common import Unishare
    import unisi.server as server
    import config

    monkeypatch.setattr(config, "session", session_id)
    monkeypatch.setattr(config, "share", True)
    first, _ = server.make_user(make_mocked_request("GET", "/ws"))
    second, _ = server.make_user(make_mocked_request("GET", "/ws"))
    assert second is not first and second.reflections is first.reflections == [first, second]
    assert Unishare.sessions[session_id] is first                   # the agent and new pages find the same session
