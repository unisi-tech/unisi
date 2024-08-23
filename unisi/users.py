from .utils import *
from .guielements import *
from .common import *
from .containers import Dialog, Screen
from .multimon import notify_monitor, logging_lock, run_external_process
from .kdb import Database
import sys, asyncio, logging, importlib

class User:          
    last_user = None
    toolbar = []
    sessions = {}
    count = 0

    def __init__(self, session: str, share = None):          
        self.session = session        
        self.active_dialog = None        
        self.last_message = None                       
        User.last_user = self

        if share:            
            self.screens = share.screens            
            self.screen_module = share.screen_module if share.screens else []
            self.__handlers__ =  share.__handlers__        
            
            if share.reflections:            
                share.reflections.append(self)
            else:
                share.reflections = [share, self]                    
            self.reflections = share.reflections
        else:
            self.screens = []        
            self.reflections = []
            self.screen_module = None         
            self.__handlers__ = {} 

        self.monitor(session, share)     

    async def run_process(self, long_running_task, *args, progress_callback = None, **kwargs):
        if progress_callback and notify_monitor and progress_callback != self.progress: #progress notifies the monitor
            async def new_callback(value):
                asyncio.gather(notify_monitor('e', self.session, self.last_message), progress_callback(value))                
            progress_callback = new_callback
        return await run_external_process(long_running_task, *args, progress_callback = progress_callback, **kwargs)

    async def broadcast(self, message):
        screen = self.screen_module
        if type(message) != str:
            message = toJson(self.prepare_result(message))  
        await asyncio.gather(*[user.send(message)
            for user in self.reflections
                if user is not self and screen is user.screen_module])        

    async def reflect(self, message, result):
        if self.reflections and not is_screen_switch(message):                        
            if result:
                await self.broadcast(result)        
            if message:                    
                msg_object = self.find_element(message)                                     
                if not isinstance(result, Message) or not result.contains(msg_object):                                                        
                    await self.broadcast(msg_object)    

    async def progress(self, str, *updates):
        """open or update progress window if str != null else close it """  
        if not self.testing:           
            msg = TypeMessage('progress', str, *updates, user = self)            
            await asyncio.gather(self.send(msg), self.reflect(None, msg))
            if notify_monitor:
                await notify_monitor('e', self.session, self.last_message) 
                                           
    def load_screen(self, file):                     
        name = file[:-3]        
        path = f'{screens_dir}{divpath}{file}'                
        spec = importlib.util.spec_from_file_location(name,path)
        module = importlib.util.module_from_spec(spec)        
        module.user = self  
                
        spec.loader.exec_module(module)            
        screen = Screen(getattr(module, 'name', ''))
        #set system vars
        for var, val in screen.defaults.items():                                            
            setattr(screen, var, getattr(module, var, val))         
        
        if screen.toolbar:
            screen.toolbar += User.toolbar
        else: 
            screen.toolbar = User.toolbar  
        module.screen = screen                                 
        return module
    
    async def delete(self):
        uss = User.sessions
        if uss and uss.get(self.session):
            del uss[self.session]
        
        if self.reflections: #reflections is common array
            if len(self.reflections) == 2: 
                self.reflections.clear() #1 element in user.reflections has no sense
            else:
                self.reflections.remove(self)  

        if notify_monitor:
            await notify_monitor('-', self.session, self.last_message)   

    def set_clean(self):
        #remove user modules from sys 
        if os.path.exists(blocks_dir):
            for file in os.listdir(blocks_dir):
                if file.endswith(".py") and file != '__init__.py':
                    name = f'{blocks_dir}.{file[0:-3]}'
                    if name in sys.modules:
                        sys.modules[name].user = self
                        del sys.modules[name]                          
    def load(self):              
        if os.path.exists(screens_dir):
            for file in os.listdir(screens_dir):
                if file.endswith(".py") and file != '__init__.py':
                    module = self.load_screen(file)                
                    self.screens.append(module)                
            
        if self.screens:
            self.screens.sort(key=lambda s: s.screen.order)            
            main = self.screens[0]
            if 'prepare' in dir(main):
                main.prepare()
            self.screen_module = main
            self.update_menu()
            self.set_clean()       
            return True                 

    def update_menu(self):
        menu = [[getattr(s, 'name', ''),getattr(s,'icon', None)] for s in self.screens]        
        for s in self.screens:
            s.screen.menu = menu

    @property
    def testing(self):        
        return  self.session == testdir
    
    @property
    def screen(self):        
        return  self.screen_module.screen 

    def set_screen(self,name):
        return asyncio.run(self.process(ArgObject(block = 'root', element = None, value = name)))
    async def result4message(self, message):
        result = None        
        self.last_message = message     
        if dialog := self.active_dialog:            
            if message.element is None: #dialog command button is pressed
                self.active_dialog = None    
                if self.reflections:            
                    await self.broadcast(TypeMessage('action', 'close'))                                    
                result = await self.eval_handler(dialog.changed, dialog, message.value)
            else:
                el = self.find_element(message)
                if el:
                    result = await self.process_element(el, message)                        
        else:
            result = await self.process(message)           
        if result and isinstance(result, Dialog):
            self.active_dialog = result
        return result

    async def eval_handler(self, handler, gui, value):
        if notify_monitor:
            await notify_monitor('+', self.session, self.last_message)        
        result = (await handler(gui, value)) if asyncio.iscoroutinefunction(handler)\
            else handler(gui, value)
        if notify_monitor:
            await notify_monitor('-', self.session, None)        
        return result

    @property
    def blocks(self):
        return [self.active_dialog] if self.active_dialog and \
            self.active_dialog.value else self.screen.blocks

    def find_element(self, message):               
        blname = message.block
        elname = message.element
        if blname == 'toolbar':
            for e in self.screen.toolbar:
                if e.name == elname:                
                    return e
        else:
            for bl in flatten(self.blocks):
                if bl.name == blname:
                    for c in flatten(bl.value):
                        if c.name == elname:
                            return c
        
    def find_path(self, elem):        
        for bl in flatten(self.blocks):        
            if bl == elem:
                return [bl.name]
            for c in flatten(bl.value):
                if c == elem:
                    return [bl.name, c.name]
        for e in self.screen.toolbar:
            if e == elem:                
                return ['toolbar', e.name]

    def prepare_result(self, raw):
        if raw is True or raw == Redesign:
            raw = self.screen      
            raw.reload = raw == Redesign                              
        else:
            if isinstance(raw, Message):
                raw.fill_paths4(self)                
            elif isinstance(raw,Gui):
                raw = Message(raw, user = self)                 
            elif isinstance(raw, (list, tuple)):
                raw = Message(*raw, user = self)
        return raw

    async def process(self, message):        
        screen_change_message = getattr(message, 'screen',None) and self.screen.name != message.screen
        if is_screen_switch(message) or screen_change_message:
            for s in self.screens:
                if s.name == message.value:
                    self.screen_module = s                    
                    if screen_change_message:
                        break                    
                    if getattr(s.screen,'prepare', False):
                        s.screen.prepare()
                    return True 
            else:        
                error = f'Unknown screen name: {message.value}'   
                self.log(error)
                return Error(error)
        
        elem = self.find_element(message)          
        if elem:                          
            return await self.process_element(elem, message)  
        
        error = f'Element {message.block}/{message.element} does not exist!'
        self.log(error)
        return Error(error)
        
    async def process_element(self, elem, message):                
        event = message.event 
        query = event == 'complete' or event == 'append' or event == 'get'        
        handler = self.__handlers__.get((elem, event), None)
        if handler:
            return await self.eval_handler(handler, elem, message.value)
                                                                    
        if hasattr(elem, event):                
            attr = getattr(elem, event)
            if is_callable(attr):
                result = await self.eval_handler(attr, elem, message.value)
                if query:                        
                    result = Answer(event, message, result)                
                return result
            #set attribute only for declared properties
            setattr(elem, event, message.value)
        elif event == 'changed':            
            elem.value = message.value                                        
        else:
            error = f"{message.block}/{message.element} doesn't contain '{event}' method type!"
            self.log(error)                     
            return Error(error)
        
    def monitor(self, session, share):
        if config.share and session != testdir:
            self.log(f'User is connected, session: {session}, share: {share.session if share else None}', type = 'info')            

    def sync_send(self, obj):                    
        asyncio.run(self.send(obj))
    
    def log(self, str, type = 'error'):        
        scr = self.screen.name if self.screens else 'void'
        str = f"session: {self.session}, screen: {scr}, message: {self.last_message}\n  {str}"
        with logging_lock:
            if type == 'error':
                logging.error(str)
            elif type == 'warning':
                logging.warning(str)    
            else:
                func = logging.getLogger().setLevel
                func(level = logging.INFO)
                logging.info(str)
                func(level = logging.WARNING)

def context_user():
    return context_object(User)

def context_screen():
    user = context_user()
    return user.screen if user else None

def message_logger(str, type = 'error'):
    user = context_user()
    user.log(str, type)

references.context_user = context_user

User.db = Database(config.db_dir, message_logger) if config.db_dir else None
User.type = User

def make_user(request):
    session = f'{request.remote}-{User.count}'
    User.count += 1    
    if requested_connect := request.query_string if config.share else None:
        user = User.sessions.get(requested_connect, None)
        if not user:
            error = f'Session id "{requested_connect}" is unknown. Connection refused!'
            with logging_lock:
                logging.error(error)
            return None, Error(error)
        user = User.type(session, user)
        ok = user.screens
    elif config.mirror and User.last_user:
        user = User.type(session, User.last_user)
        ok = user.screens
    else:
        user = User.type(session)
        ok = user.load()       
    User.sessions[session] = user 
    return user, ok

def handle(elem, event):
    def h(fn):
        key = elem, event
        handler_map = User.last_user.__handlers__        
        func = handler_map.get(key, None)        
        if func:
            handler_map[key] =  compose_handlers(func, fn)  
        else: 
            handler_map[key] = fn
        return fn
    return h

references.handle = handle