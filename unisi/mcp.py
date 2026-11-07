# Copyright © 2024 UNISI Tech. All rights reserved.
"""Agent API: the Unified Remote API for AI agents -- an MCP server inside the UNISI server (`config.mcp = True`).

An agent (Claude Code: `claude mcp add --transport http app 'http://localhost:8000/mcp?session=<id>'`) works in a
live session as one more view of it: what its tools change reaches every view of the session at once, and what a
person changes it reads at its next call. Transport: MCP Streamable HTTP with JSON answers (no SSE stream), local
connections only.

Tools: the general ones below (the app's screens and units, input as a person's, the open dialog, holding hot reload)
and the app's own -- `tools = [fn, ...]` of a screen module: functions of this user's module instance, like handlers
(`user` is theirs), the input schema from their parameter hints (the types of Q), the description from the docstring.
Their result goes to the agent; the units they change, to the views."""
import functools, inspect, json, re
from typing import Any, get_type_hints
from aiohttp import web
from .common import Unishare, ReceivedMessage, Message, TypeMessage, flatten, toJson, call_anysync
from .containers import Dialog
from .llmrag import _type_to_schema_dict
from .reloader import hold_reload
from .sessions import open_session, session_registry
from .utils import app_path, screens_dir
import config

LOCAL = ('127.0.0.1', '::1', '::ffff:127.0.0.1')
PROTOCOL = '2025-06-18'
LONG = 200          # ui_view shows a longer text by its size; ui_get gives it whole


def session_user(session):
    """The user of the session the agent works in: its live (first) user, or the saved session resumed for the
    agent without a page -- a page coming to it later takes it over (server.make_user). A str: why there is none."""
    if not session:
        return 'No session: /mcp?session=<id> (or config.session)'
    if user := Unishare.sessions.get(session):
        return user
    if not session_registry.resumable(session):
        return f'Unknown session "{session}"'
    user = open_session(session)
    user.agent_only = True              # a page coming to the session takes this user over (server.make_user)

    async def quiet(res, persist = True):      # no page: what changes is only saved
        return res if type(res) == str else user.prepare_result(res, persist = persist)
    user.send = quiet
    return user


# ── general tools: the first parameter is the session's user, the agent gives the rest ──────────────────────────

def ui_screens(user):
    """The screens of the app: their names, and which one the person sees now (`current`)."""
    current = user.screen_module.screen.name if user.screen_module else None
    return [{'name': info.name, 'current': info.name == current} for info in user.screen_registry]


def ui_view(user, screen: str = ''):
    """The units of a screen ('' -- the one the person sees) at a glance: blocks with their units -- name, type,
    value; a long text only by its size, a table by its headers, row count and selection. A dialog open in the
    session comes first. A unit whole -- ui_get."""
    module = _screen(user, screen)
    return {'screen': module.screen.name, 'blocks': [brief(b) for b in _tops(user, module)],
            'toolbar': [brief(u) for u in module.screen.toolbar]}


def ui_get(user, unit: str, screen: str = ''):
    """A unit whole, as a page gets it: a table with its rows, a text with all of it."""
    return json.loads(toJson(_find(user, _screen(user, screen), unit)[0]))


async def ui_set(user, unit: str, value: Any, screen: str = ''):
    """Enter a value into a unit as the person would: its `changed` handler runs (for a table the value is the
    selection: a row index or a list of them). The person sees at once what the handler answers and changes;
    you get its answer and the names of the changed units."""
    return await _act(user, screen, unit, value)


async def ui_push(user, button: str, screen: str = ''):
    """Press a button as the person would (one of the toolbar too). You get its answer and the names of the
    changed units; a dialog it opens is the session's -- answer it with ui_dialog."""
    return await _act(user, screen, button, None)


async def ui_dialog(user, command: str | None = None):
    """Answer the dialog open in the session with one of its commands (None -- closed without one, as by Esc);
    the person's views close it too. Its fields -- ui_set first."""
    dialog = user.active_dialog
    if not dialog:
        raise ValueError('No dialog is open in the session')
    if command is not None and command not in dialog.commands:
        raise ValueError(f'The dialog has no command "{command}": {dialog.commands}')
    await user.send(TypeMessage('action', 'close'), persist = False)    # before the callback, as result4message tells the other views
    return await user.result4message(ReceivedMessage({'path': [dialog.name], 'event': 'changed', 'value': command}))


_held = False

def code_hold(user, on: bool):
    """Hold hot reload while you edit the app's source files (on=True) and release it when they are done and
    tested (False): the app then reloads once, not at every saved file, and a running job keeps its modules."""
    global _held
    if on != _held:
        hold_reload(on)
        _held = on
    return {'held': _held}


GENERAL = {fn.__name__: fn for fn in (ui_screens, ui_view, ui_get, ui_set, ui_push, ui_dialog, code_hold)}
ACTING = (ui_set, ui_push, ui_dialog)      # their answer is a handler's: it goes to the views too


def brief(unit) -> dict:
    """A unit at a glance: name, type and value -- a long text by its size, a table by its headers, row count and
    selection, a block or a dialog by its units."""
    d = {'name': unit.name, 'type': unit.type}
    if unit.type in ('block', 'dialog'):
        d['units'] = [brief(u) for u in flatten(unit.value) if hasattr(u, 'type')]
    elif unit.type == 'table':
        d.update(headers = getattr(unit, 'headers', None), rows = len(getattr(unit, 'rows', None) or []), value = unit.value)
    elif isinstance(v := getattr(unit, 'value', None), str) and len(v) > LONG:
        d['chars'] = len(v)
    elif v is not None and v != '':
        d['value'] = v
    return d


def _screen(user, screen):
    module = user.ensure_screen(screen) if screen else user.screen_module
    if not module:
        raise ValueError(f'No screen "{screen}": {[i.name for i in user.screen_registry]}')
    return module


def _tops(user, module):
    return [*([user.active_dialog] if user.active_dialog else []), *flatten(module.screen.blocks)]


def _find(user, module, name):
    """The unit `name` of the screen (of the dialog open in the session first) and its path, as a page addresses it."""
    def walk(items, path):
        for u in flatten(items):
            if getattr(u, 'name', None) == name:
                return u, [name, *path]
            if getattr(u, 'type', None) == 'block' and (found := walk(u.value, [u.name, *path])):
                return found
    for top in _tops(user, module):
        if found := (top, [name]) if top.name == name else walk(top.value, [top.name]):
            return found
    for u in module.screen.toolbar:
        if u.name == name:
            return u, [name, 'toolbar']
    raise ValueError(f'No unit "{name}" on the screen {module.screen.name}')


async def _act(user, screen, name, value):
    unit, path = _find(user, _screen(user, screen), name)
    result = await user.process_element(unit, ReceivedMessage({'path': path, 'event': 'changed', 'value': value}))
    if isinstance(result, Dialog):
        user.active_dialog = result
    return result


# ── the app's tools ─────────────────────────────────────────────────────────────────────────────────────────────

_TOOLS = re.compile(r'^tools\s*=', re.M)

def app_tools(user) -> dict:
    """`tools` of the app's screens, from this user's module instances (a screen that declares them gets loaded)."""
    tools = {}
    for info in user.screen_registry:
        try:
            with open(app_path(screens_dir, info.file)) as f:
                declares = _TOOLS.search(f.read())
        except OSError:
            continue
        if declares and (module := user.ensure_screen(info.name or info.file)):
            tools.update((fn.__name__, fn) for fn in getattr(module, 'tools', []))
    return tools


def describe(name, fn) -> dict:
    """A tool for tools/list: the input schema from the parameter hints (the types of Q; no hint -- any value),
    a parameter with a default optional; the user of a general tool is not the agent's to give."""
    hints = get_type_hints(fn)
    params = list(inspect.signature(fn).parameters.values())[1 if GENERAL.get(name) is fn else 0:]
    props = {p.name: {} if hints.get(p.name, Any) is Any else _type_to_schema_dict(hints[p.name]) for p in params}
    return {'name': name, 'description': inspect.getdoc(fn) or name,
            'inputSchema': {'type': 'object', 'properties': props, 'required': [p.name for p in params if p.default is p.empty]}}


async def _call(user, fn, args):
    """A tool in the context of the session's user (context_user finds him: he is the first argument here)."""
    # a unit the call changes is sent to the views, not taken for the echo of a page's own edit
    user.handling(ReceivedMessage({'path': ['mcp'], 'event': fn.__name__, 'value': None}))
    call = functools.partial(fn, user, **args) if GENERAL.get(fn.__name__) is fn else functools.partial(fn, **args)
    return await call_anysync(call)


async def deliver(user, result = None) -> list:
    """What a call changed -> every view of the session at once: the units (each view gets those of its screen) and
    the answer of a handler (a message, a dialog). A unit tells the view that compiled its screen, so the changes of
    all the views are taken. Returns the names of the changed units."""
    if units := user.session_changes():         # saved with the session's user and shown in its view, then in the others
        await user.send(None)
        await user.share_units(units)
    if result is not None:                      # after the units: the client shows a dialog without applying updates
        for v in user.reflections or [user]:
            await v.send(result, persist = v is user)
    return [u.name for u in units]


def _answer(result):
    """What a handler answered, for the agent: a message by its type and text, a dialog by its question and commands."""
    if isinstance(result, Dialog):
        return {'dialog': result.name, 'commands': result.commands}
    if isinstance(result, Message):
        return {'type': result.type, 'text': getattr(result, 'value', None)}
    return result if result is None or isinstance(result, (str, int, float, bool)) else str(result)


def _text(result) -> str:
    """The agent's text of a result: as is, or compact JSON with the letters themselves (toJson escapes non-ASCII)."""
    return result if isinstance(result, str) else json.dumps(json.loads(toJson(result)), ensure_ascii = False)


def _reply(mid, result = None, error = None):
    body = {'jsonrpc': '2.0', 'id': mid, **({'error': error} if error else {'result': result})}
    return web.Response(text = json.dumps(body, ensure_ascii = False), content_type = 'application/json')


async def handler(request):
    """/mcp: one JSON-RPC message of MCP per POST; GET (a stream of the server's own messages) is not offered."""
    if request.method != 'POST':
        return web.Response(status = 405)
    if request.remote not in LOCAL:
        return web.Response(status = 403)
    try:
        msg = await request.json()
    except ValueError:
        return _reply(None, error = {'code': -32700, 'message': 'Parse error'})
    mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
    if mid is None:                     # a notification: initialized, cancelled
        return web.Response(status = 202)
    if method == 'initialize':
        return _reply(mid, {'protocolVersion': params.get('protocolVersion', PROTOCOL), 'capabilities': {'tools': {}},
                            'serverInfo': {'name': config.appname, 'version': '1'}})
    if method == 'ping':
        return _reply(mid, {})
    if method not in ('tools/list', 'tools/call'):      # server/discover too: Claude Code asks it first
        return _reply(mid, error = {'code': -32601, 'message': f'Method not found: {method}'})
    user = session_user(request.query.get('session') or config.session)
    if isinstance(user, str):
        return _reply(mid, error = {'code': -32602, 'message': user})
    tools = {**GENERAL, **app_tools(user)}
    if method == 'tools/list':
        return _reply(mid, {'tools': [describe(name, fn) for name, fn in tools.items()]})
    if (fn := tools.get(params.get('name'))) is None:
        return _reply(mid, error = {'code': -32602, 'message': f'Unknown tool: {params.get("name")}'})
    try:
        result, error = await _call(user, fn, params.get('arguments') or {}), False
    except Exception as e:
        result, error = f'{type(e).__name__}: {e}', True
    acting = fn in ACTING and not error
    changed = await deliver(user, result if acting else None)
    if acting:
        result = {'answer': _answer(result), 'changed': changed}
    return _reply(mid, {'content': [{'type': 'text', 'text': _text(result)}], 'isError': error})
