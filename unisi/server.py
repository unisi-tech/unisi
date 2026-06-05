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
from config import port, upload_dir
import traceback, json, random, string
from urllib.parse import parse_qs

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
    else:
        user_id = parsed_query.get('id', [User.count])[0]
        session = f'{generate_random_string()}-{user_id}'      
          
    if config.share and 'session' in parsed_query:
        user = Unishare.sessions.get(session, None)
        if not user:
            error = f'Session id "{session}" is unknown. Connection refused!'
            with logging_lock:
                logging.error(error)
            return None, Error(error)
        user = User.type(session, user, screen=requested_screen)
        ok = user.screens
    elif config.mirror and User.count:
        user = User.type(session, User.last_user, screen=requested_screen)
        ok = user.screens
    else:
        user = User.type(session, screen=requested_screen)
        ok = user.screens
    User.count += 1
    Unishare.sessions[session] = user 
    return user, ok

def handle(unit, event):
    handler_map = User.last_user.handlers        
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
        return web.HTTPBadRequest(text='No file provided')
    # Use only the basename — prevents path traversal via crafted filenames like ../../etc/passwd
    safe_name = Path(field.filename).name
    filename = upload_path(safe_name)
    with open(filename, 'wb') as f:
        while True:
            chunk = await field.read_chunk()  
            if not chunk:
                break
            f.write(chunk)
    return web.Response(text=filename)

async def static_serve(request: web.Request) -> web.StreamResponse:
    rpath = request.path

    if rpath == '/':
        rpath = '/index.html'

    # 1. Serve from webpath with path traversal protection
    try:
        base_webpath = Path(webpath).resolve()
        file_path = (Path(webpath) / rpath.lstrip('/')).resolve()
        if file_path.is_relative_to(base_webpath) and file_path.exists():
            return web.FileResponse(file_path)
    except (ValueError, RuntimeError):
        pass

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

    return web.HTTPNotFound()
     
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)    
    user, status = make_user(request)
    if not user:
        await ws.send_str(toJson(status))
    else:
        async def send(res):
            if type(res) != str:
                res = toJson(user.prepare_result(res))        
            await ws.send_str(res)        
        user.send = send         
        await send(True if status else empty_app) 
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data == 'close':
                        await ws.close()
                    else:
                        raw_message = json.loads(msg.data)
                        message = None
                        if isinstance(raw_message, list):
                            if raw_message:
                                for raw_submessage in raw_message:
                                    message = ReceivedMessage(raw_submessage)                    
                                    result = await user.result4message(message)
                            else:                                
                                result = Warning('Empty command batch!')
                        else:                    
                            message = ReceivedMessage(raw_message)            
                            result = await user.result4message(message)                    
                        await send(result)
                        if message:
                            if recorder.record_file:
                                recorder.accept(message, user.prepare_result (result))
                            await user.reflect(message, result)     
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

def start(user_type = User, http_handlers = []):    
    ensure_directory_exists(screens_dir)
    ensure_directory_exists(blocks_dir)
    setup_llmrag()

    User.type = user_type        
    run_tests(User.init_user())
                        #http_handlers has to be the first argument
    server_handlers = http_handlers + [web.get('/ws', websocket_handler), 
            web.static(f'/{config.upload_dir}', upload_dir), 
        web.get('/{tail:.*}', static_serve), web.post('/', post_handler)] 

    app = web.Application()
    app.add_routes(server_handlers)    
    web.run_app(app, port = port)