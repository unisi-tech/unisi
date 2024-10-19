# Copyright © 2024 UNISI Tech. All rights reserved.
from aiohttp import web, WSMsgType
from .users import *
from pathlib import Path
from .reloader import empty_app 
from .autotest import recorder, run_tests
from .common import  *
from .llmrag import setup_llmrag
from .dbunits import dbupdates
from .kdb import Database
from config import port, upload_dir
import traceback, json

def context_user():
    return context_object(User)

def context_screen():
    user = context_user()
    return user.screen if user else None

def message_logger(str, type = 'error'):
    user = context_user()
    user.log(str, type)

Unishare.context_user = context_user
Unishare.message_logger = message_logger
User.type = User    

if config.db_dir:
    Unishare.db = Database(config.db_dir, message_logger) 

def make_user(request):
    session = f'{request.remote}-{User.count}'        
    if requested_connect := request.query_string if config.share else None:
        user = Unishare.sessions.get(requested_connect, None)
        if not user:
            error = f'Session id "{requested_connect}" is unknown. Connection refused!'
            with logging_lock:
                logging.error(error)
            return None, Error(error)
        user = User.type(session, user)
        ok = user.screens
    elif config.mirror and User.count:
        user = User.type(session, User.last_user)
        ok = user.screens
    elif not User.count:
        user = User.last_user        
        user.session = session
        user.monitor(session)
        ok = True
    else:
        user = User.type(session)
        ok = user.load()                  
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

async def post_handler(request):
    reader = await request.multipart()
    field = await reader.next()   
    filename = upload_path(field.filename)      
    size = 0
    with open(filename, 'wb') as f:
        while True:
            chunk = await field.read_chunk()  
            if not chunk:
                break
            size += len(chunk)
            f.write(chunk)
    return web.Response(text=filename)

async def static_serve(request):    
    rpath = request.path    
    file_path  = Path(f"{webpath}{rpath}" )
    if request.path == '/':
        file_path /= 'index.html'
    if not file_path.exists():
        file_path = None
        #unmask win path
        if rpath.startswith('/') and rpath[2] == ':':
            rpath = rpath[1:]        
        for dir in config.public_dirs:              
            if rpath.startswith(dir):                
                if os.path.exists(rpath):
                    file_path  = Path(rpath)
                break            
    return web.FileResponse(file_path) if file_path else web.HTTPNotFound()
     
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
        except BaseException as e:
            if not isinstance(e, ConnectionResetError):
                user.log(traceback.format_exc())
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

    http_handlers.insert(0, web.get('/ws', websocket_handler))        
    http_handlers += [web.static(f'/{config.upload_dir}', upload_dir), 
        web.get('/{tail:.*}', static_serve), web.post('/', post_handler)]

    app = web.Application()
    app.add_routes(http_handlers)    
    web.run_app(app, port = port)
    
