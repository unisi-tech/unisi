from aiohttp import web, WSMsgType
from .users import *
from pathlib import Path
from .reloader import empty_app 
from .autotest import recorder, run_tests
from .common import  *
from config import port, upload_dir
import traceback, json

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
        dirs = getattr(config, public_dirs, []) 
        for dir in dirs:              
            if rpath.startswith(dir):                
                if os.path.exists(rpath):
                    file_path  = Path(rpath)
                break
            
    return web.FileResponse(file_path) if file_path else web.HTTPNotFound()
     
async def broadcast(message, message_user):
    screen = message_user.screen_module
    await asyncio.gather(*[user.send(message)
        for user in message_user.reflections
            if user is not message_user and screen is user.screen_module])            

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    user, status = make_user(request)
    if not user:
        await ws.send_str(toJson(status))
    else:
        user.transport = ws._writer.transport  if divpath != '/' else None          

        async def send(res):
            if type(res) != str:
                res = toJson(user.prepare_result(res))        
            await ws.send_str(res)        

        user.send = send         
        await send(user.screen if status else empty_app) 
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
                            if user.reflections and not is_screen_switch(message):                        
                                if result:
                                    await broadcast(result, user)                            
                                msg_object = user.find_element(message)                         
                                if not isinstance(result, Message) or not result.contains(msg_object):                                                        
                                    await broadcast(toJson(user.prepare_result(msg_object)), user)
                elif msg.type == WSMsgType.ERROR:
                    user.log('ws connection closed with exception %s' % ws.exception())
        except:        
            user.log(traceback.format_exc())

        uss = User.sessions
        if uss and uss.get(user.session):
            del uss[user.session]
        
        if user.reflections: #reflections is common array
            if len(user.reflections) == 2: 
                user.reflections.clear() #1 element in user.reflections has no sense
            else:
                user.reflections.remove(user)        
    return ws  #?<->     

def start(appname = None, user_type = User, http_handlers = []):    
    if appname is not None:
        config.appname = appname

    User.type = user_type    

    if config.autotest:
        run_tests()

    http_handlers.insert(0, web.get('/ws', websocket_handler))        
    http_handlers += [web.static(f'/{config.upload_dir}', upload_dir), 
        web.get('/{tail:.*}', static_serve), web.post('/', post_handler)]

    print(f'Start {appname} web server..')    
    app = web.Application()
    app.add_routes(http_handlers)    
    web.run_app(app,  port=port)
    
