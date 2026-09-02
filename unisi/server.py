# Copyright © 2024 UNISI Tech. All rights reserved.
from aiohttp import web, WSMsgType
from .users import *
from pathlib import Path
from .reloader import active_reloader  # noqa: F401 — imported for side-effect (starts reloader)
from .autotest import recorder, run_tests
from .common import  *
from .llmrag import setup_llmrag
from .dbunits import dbupdates
from .db import db 
import traceback, json, random, string, os
from urllib.parse import parse_qs
import config

def generate_random_string(length=10):
    characters = string.ascii_letters + string.digits 
    return ''.join(random.choices(characters, k=length))

def context_user():
    return context_object(User)

def context_screen():
    user = context_user()
    return user.screen if user else None

def message_logger(message, type = 'error'):
    user = context_user()
    if user:    
        user.log(message, type)
    else:
        # No user in context yet (e.g. a startup-time warning from a
        # module that runs before any User exists) -- always logs at
        # error level regardless of `type`, by design: start_logging()
        # configures the root logger at WARNING, so an 'info'-level
        # message here would otherwise be silently dropped instead of
        # shown. See llmrag.py's setup_llmrag() docstring for the
        # concrete case this exists for.
        with logging_lock:
            logging.error(message)

Unishare.context_user = context_user
Unishare.context_screen = context_screen
Unishare.message_logger = message_logger
User.type = User    

if db:
    Unishare.db = db

def make_user(request):    
    parsed_query = parse_qs(request.query_string)
    requested_screen = parsed_query.get('screen', [None])[0]
    if 'session' in parsed_query:
        session = parsed_query['session'][0]
        parts = session.split('-')
        user_id = parts[1] if len(parts) > 1 else parts[0]
    elif config.session:
        session = config.session
    else:
        user_id = parsed_query.get('id', [User.count])[0]
        session = f'{generate_random_string()}-{user_id}'      
          
    register = True
    if config.share and 'session' in parsed_query:
        root = Unishare.sessions.get(session, None)
        if not root:
            error = f'Session id "{session}" is unknown. Connection refused!'
            with logging_lock:
                logging.error(error)
            return None, Error(error)
        user = User.type(session, root, screen=requested_screen)
        ok = user.screens
        # Don't overwrite Unishare.sessions[session]: it must keep pointing
        # at `root`, the stable session every future share='session' lookup
        # (and the reflections group itself) is anchored to. Registering
        # `user` (a transient proxy/reflection) here instead makes the NEXT
        # such connect share=this one rather than the real root -- and once
        # this one disconnects, that next connect's own reflections list
        # gets rebuilt from a dead object, silently dropping the root out of
        # the broadcast group.
        register = False
    elif config.mirror and User.count:
        user = User.type(session, User.last_user, screen=requested_screen)
        ok = user.screens
    else:
        user = User.type(session, screen=requested_screen)
        ok = user.screens
    User.count += 1
    if register:
        Unishare.sessions[session] = user 
    return user, ok

def handle(unit, event):
    # Resolve "the current user" primarily from the real call stack
    # (context_user()), not from a process-wide "last constructed"
    # pointer. compile_screen() runs synchronously as a method on the
    # exact User whose screen is being (lazily) loaded -- via
    # User.__init__ for a user's first screen, or via
    # ensure_screen()/screen_process() for any screen an already-connected
    # user navigates to later -- so that User's frame is reliably on the
    # stack here, regardless of how many other Users are concurrently
    # connected/active. User.last_user only tracks the most recently
    # *constructed* user process-wide: correct for a user's own first
    # screen (set immediately before that synchronous load), but stale
    # the moment another User is constructed afterwards -- so any screen a
    # previously-connected user lazily loads later used to attribute its
    # handlers to whoever happened to be last_user *now*, not to the user
    # actually loading that screen.
    #
    # `or User.last_user`: context_user() finds nothing when handle() (or
    # a persistent Table(), which calls it from Table.__init__ -- see
    # tables.py) runs with no User anywhere on the stack -- e.g. a
    # module-level Table() imported before unisi.start() has created
    # anyone, or one built directly by a unit test with no real screen
    # load involved. User.last_user remains the fallback there, same as
    # before: falsy before the first User ever exists (still routes to
    # Unishare.pending_handlers below), or whatever the caller has wired
    # up as "the current user" otherwise (a real User for a Table()
    # declared inside a screen module but reached via some indirection
    # context_user() doesn't see, or a duck-typed test double). This
    # fallback never overrides a real context_user() hit -- `or` only
    # reaches it when the stack genuinely has no User on it.
    user = context_user() or User.last_user
    handler_map = user.handlers if user else Unishare.pending_handlers
    def h(fn):
        key = unit, event        
        func = handler_map.get(key, None)        
        if func:
            handler_map[key] =  compose_handlers(func, fn)  
        else: 
            handler_map[key] = fn
        return fn
    return h

Unishare.handle = handle

Unishare.test_list = []

def test(fn):    
    Unishare.test_list.append(fn)
    return fn

async def post_handler(request):
    reader = await request.multipart()
    field = await reader.next()
    if not field or not getattr(field, 'filename', None):
        # raise, not return -- aiohttp deprecated returning an
        # HTTPException object (#2415) in favor of raising it; both
        # currently produce the identical response (same status/body),
        # but only raising is forward-compatible.
        raise web.HTTPBadRequest(text='No file provided')
    # Use only the basename — prevents path traversal via crafted filenames like ../../etc/passwd
    safe_name = Path(field.filename).name
    if not safe_name or safe_name == '..':
        # Path(...).name is '' for inputs like '', '.', or '/', but for a
        # bare '..' it stays '..' (pathlib treats it as an ordinary last
        # segment, not something to resolve away) -- either way, joining
        # it with upload_dir points at an *existing directory* (upload_dir
        # itself, or its parent), and open(that, 'wb') below would raise
        # an unhandled IsADirectoryError (a 500) instead of a clean 400.
        raise web.HTTPBadRequest(text='Invalid filename')
    filename = upload_path(safe_name)
    with open(filename, 'wb') as f:
        while True:
            chunk = await field.read_chunk()  
            if not chunk:
                break
            f.write(chunk)
    return web.Response(text=filename)

# The framework's bundled default web client always stays reachable under
# this path, however config.web_client is set -- see static_serve() below.
DEFAULT_CLIENT_ROUTE = '/default'

def active_webpath():
    """Directory currently served at '/': config.web_client when a custom
    UNISI-protocol web client is configured, otherwise the framework's own
    bundled client (webpath). The bundled client itself is unaffected by
    this setting -- it always stays reachable at DEFAULT_CLIENT_ROUTE, see
    static_serve().
    """
    return config.web_client or webpath

def resolve_in_root(root, rpath):
    """Resolve rpath (a request path, e.g. '/js/app.js') to a file inside
    root, with path traversal protection. Returns the resolved Path if it
    exists inside root, else None. root may be relative (resolved against
    the current working directory, matching config.public_dirs) or
    absolute, and need not exist -- a missing/invalid root simply yields no
    matches, it never raises.
    """
    try:
        base = Path(root).resolve()
        file_path = (base / rpath.lstrip('/')).resolve()
        if file_path.is_relative_to(base) and file_path.exists():
            return file_path
    except (ValueError, RuntimeError):
        pass
    return None

async def static_serve(request: web.Request) -> web.StreamResponse:
    rpath = request.path

    # The bundled default client stays reachable at /default (and anything
    # under it) no matter what config.web_client is set to -- resolved
    # against `webpath` specifically, never the active/custom root, so it
    # keeps working as a fixed reference UI regardless of configuration.
    # This intentionally reserves /default: a custom web_client cannot
    # serve its own content at that exact path.
    if rpath == DEFAULT_CLIENT_ROUTE or rpath.startswith(f'{DEFAULT_CLIENT_ROUTE}/'):
        sub_path = rpath[len(DEFAULT_CLIENT_ROUTE):] or '/'
        if sub_path == '/':
            sub_path = '/index.html'
        file_path = resolve_in_root(webpath, sub_path)
        if file_path:
            return web.FileResponse(file_path)
        raise web.HTTPNotFound()

    if rpath == '/':
        rpath = '/index.html'

    # 1. Serve from the active web client (config.web_client if a custom
    # UNISI-protocol client is configured, otherwise the bundled default)
    # with path traversal protection.
    file_path = resolve_in_root(active_webpath(), rpath)
    if file_path:
        return web.FileResponse(file_path)

    # 1b. A custom web_client is configured but doesn't have this file --
    # fall back to the bundled client's own copy before giving up on it.
    # Without this, a custom client could never fully own '/': the bundled
    # client's build hardcodes absolute, root-relative asset paths (e.g.
    # /js/<hash>.js) that keep getting requested from '/' even while the
    # page itself is being viewed through /default, since a <base> tag
    # only affects relative URLs. This same fallback is what makes those
    # requests resolve correctly, and it also lets a custom client
    # knowingly omit files (icons, fonts, favicon...) it's happy to
    # inherit from the bundled one. A misconfigured web_client (missing or
    # wrong path) degrades gracefully the same way: every lookup in it
    # simply misses, so '/' ends up fully served by the bundled client.
    if config.web_client:
        file_path = resolve_in_root(webpath, rpath)
        if file_path:
            return web.FileResponse(file_path)

    # 2. Serve from public_dirs (with Windows path unmasking)
    # unmask win path: /C:/public/img.png -> C:/public/img.png
    if rpath.startswith('/') and len(rpath) > 2 and rpath[2] == ':':
        rpath = rpath[1:]
    try:
        target_path = Path(rpath).resolve()
        for directory in config.public_dirs:
            dir_path = Path(directory).resolve()
            if target_path.is_relative_to(dir_path):
                # First matching dir is authoritative — don't fall through to others
                if target_path.exists():
                    return web.FileResponse(target_path)
                break
    except (ValueError, RuntimeError):
        pass

    # raise, not return -- see the matching comment in post_handler above.
    raise web.HTTPNotFound()
     
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)    
    user, status = make_user(request)
    if not user:
        await ws.send_str(toJson(status))
    else:
        async def send(res, persist=True):
            prepared = res
            try:
                if type(res) != str:
                    prepared = user.prepare_result(res, persist=persist)
                    res = toJson(prepared)
                await ws.send_str(res)
            except:
                pass
            return prepared

        user.send = send         

        await send(True if status else empty_app) 
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data == 'close':
                        # Legacy, non-JSON escape hatch -- kept so an
                        # existing client that still sends this bare
                        # string keeps working. {"path": ["close"]} below
                        # is the real, documented (protocol.md) way to
                        # ask for a clean close; every other client<->
                        # server message is JSON, this should be too.
                        await ws.close()
                    else:
                        raw_message = json.loads(msg.data)
                        message = None
                        if isinstance(raw_message, list):
                            if raw_message:
                                # `result` deliberately keeps the *last
                                # non-None* sub-result rather than
                                # unconditionally overwriting it every
                                # iteration: most handlers return None
                                # (their real effect is tracked via
                                # changed_units/touched_units and merged
                                # into one combined update by send() below),
                                # but a handler can also return an
                                # explicit Error/Warning/Info/Message --
                                # e.g. to reject one bad item partway
                                # through a batch. Unconditionally
                                # overwriting used to let a later message
                                # that returns None silently erase an
                                # earlier explicit result, so the client
                                # would never learn about it.
                                result = None
                                for raw_submessage in raw_message:
                                    message = ReceivedMessage(raw_submessage)
                                    sub_result = await user.result4message(message)
                                    if sub_result is not None:
                                        result = sub_result
                            else:                                
                                result = Warning('Empty command batch!')
                        else:                    
                            message = ReceivedMessage(raw_message)
                            if message.close_type:
                                await ws.close()
                                continue
                            result = await user.result4message(message)                    
                        prepared = await send(result)
                        if message:
                            if recorder.record_file:
                                # Reuse the exact object send() just prepared (and sent)
                                # instead of calling prepare_result() a second time: that
                                # function unconditionally drains changed_units/touched_units
                                # before returning (see its own docstring), so a second call
                                # on the same raw `result` would see those already emptied by
                                # the call send() just made and silently capture an
                                # incomplete/None response instead of what the client
                                # actually received -- which used to make recorded autotest
                                # fixtures wrong for exactly the common case (a handler
                                # returning None, with the real update carried via
                                # changed_units) that autotest exists to verify.
                                recorder.accept(message, prepared)
                            # persist=False: same reason -- changed_units/touched_units are
                            # already drained by send(result), so a real persist pass here
                            # would just be recomputing keyed-persist keys against nothing.
                            await user.reflect(message, result, persist=False)     
                        if dbupdates:
                            await user.sync_dbupdates()                       
                elif msg.type == WSMsgType.ERROR:
                    user.log('ws connection closed with exception %s' % ws.exception())
        except ConnectionResetError:
            pass
        except Exception as e:
            user.log(traceback.format_exc())
        finally:
            await user.delete()
    return ws     

def ensure_directory_exists(directory_path):
    if not os.path.exists(directory_path):
        os.makedirs(directory_path)
        print(f"Directory '{directory_path}' created.")

def ensure_unisi_typings():
    """
    Create or update __builtins__.pyi for
    static analysis support 'user' keyword (Pylance/Pyright).
    """
    typings_dir = "typings"
    builtins_file_path = "typings/__builtins__.pyi"
    
    ensure_directory_exists(typings_dir)
    builtins_content = "from unisi import User\nuser: User\n"

    try:
        needs_write = True
        if os.path.exists(builtins_file_path):
            with open(builtins_file_path, "r", encoding="utf-8") as f:
                needs_write = f.read() != builtins_content
        if needs_write:
            with open(builtins_file_path, "w", encoding="utf-8") as f:
                f.write(builtins_content)
            print(f"File '{builtins_file_path}' created/updated for Pylance support.")        
    except Exception as e:
        print(f"Error creating/updating '{builtins_file_path}': {e}")

def warn_if_web_client_misconfigured():
    """Print a startup warning if config.web_client is set but doesn't look
    like a servable UNISI web client (no index.html at its root).

    This is advisory only: static_serve()'s fallback (step 1b) already
    means '/' keeps serving the bundled default client for any file a
    misconfigured web_client doesn't provide, so this never blocks
    startup -- it just helps catch a wrong path in config.py early instead
    of silently always falling back.
    """
    if config.web_client and not (Path(config.web_client) / 'index.html').is_file():
        print(f"web_client '{config.web_client}' in config.py has no index.html. "
              f"'/' will keep serving the bundled default client "
              f"(also always available at {DEFAULT_CLIENT_ROUTE}) until this is fixed.")

def start(user_type = User, http_handlers = None):    
    # mutable-default-argument pitfall: a `[]` default here is only safe
    # because it's never mutated in place (`http_handlers + [...]` below
    # always builds a new list) -- start() also only ever runs once per
    # process (web.run_app blocks forever), so the classic "leaks between
    # calls" failure mode can't actually happen today. Still worth the
    # standard None-default fix: it costs nothing and removes a pattern
    # every linter flags on sight, for a function whose contract (append
    # your own routes) invites exactly the kind of caller who might one
    # day be tempted to `http_handlers.append(...)` before calling start().
    if http_handlers is None:
        http_handlers = []
    ensure_directory_exists(screens_dir)
    ensure_directory_exists(blocks_dir)
    ensure_unisi_typings()
    warn_if_web_client_misconfigured()
    setup_llmrag()

    User.type = user_type        
    run_tests(User.init_user())
                        #http_handlers has to be the first argument
    server_handlers = http_handlers + [web.get('/ws', websocket_handler), 
            web.static(f'/{config.upload_dir}', config.upload_dir), 
        web.get('/{tail:.*}', static_serve), web.post('/', post_handler)] 

    app = web.Application()
    app.add_routes(server_handlers)    
    web.run_app(app, port = config.port)