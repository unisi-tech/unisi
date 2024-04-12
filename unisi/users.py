from .utils import *
from .guielements import *
from .common import *
from .containers import Dialog, Screen
import sys, asyncio, logging, importlib

class User:      
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

    def sync_send(self, obj):                    
        asyncio.run(self.send(obj))

    async def broadcast(self, message):
        screen = self.screen_module
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
                    await self.broadcast(toJson(self.prepare_result(msg_object)))    

    async def progress(self, str, *updates):
        """open or update progress window if str != null else close it """  
        if not self.testing:           
            msg = TypeMessage('progress', str, *updates, user = self)            
            await asyncio.gather(self.send(msg), self.reflect(None, msg))
                                           
    def load_screen(self, file):
        screen_vars = {
            'icon' : None,
            'prepare' : None,            
            'blocks' : [],
            'header' : config.appname,                        
            'toolbar' : [], 
            'order' : 0,
            'reload': config.hot_reload 
        }             
        name = file[:-3]        
        path = f'{screens_dir}{divpath}{file}'                
        spec = importlib.util.spec_from_file_location(name,path)
        module = importlib.util.module_from_spec(spec)        
                
        module.user = self                               
        
        spec.loader.exec_module(module)            
        screen = Screen(getattr(module, 'name', ''))
        #set system vars
        for var in screen_vars:                                            
            setattr(screen, var, getattr(module,var,screen_vars[var]))         
        
        if screen.toolbar:
            screen.toolbar += User.toolbar
        else: 
            screen.toolbar = User.toolbar  
                                
        module.screen = screen        
        return module

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
        return  self.session == 'autotest'
    
    @property
    def screen(self):        
        return  self.screen_module.screen 

    def set_screen(self,name):
        return asyncio.run(self.process(ArgObject(block = 'root', element = None, value = name)))

    async def result4message(self, message):
        result = None
        dialog = self.active_dialog
        if dialog:            
            if message.element is None: #button pressed
                self.active_dialog = None    
                if self.reflections:            
                    await self.broadcast(TypeMessage('action', 'close'))
                handler = dialog.changed
                result = (await handler(dialog, message.value)) if asyncio.iscoroutinefunction(handler)\
                      else handler(dialog, message.value)
            else:
                el = self.find_element(message)
                if el:
                    result = await self.process_element(el, message)                        
        else:
            result = await self.process(message)           
        if result and isinstance(result, Dialog):
            self.active_dialog = result
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
        self.last_message = message     
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
        query = event == 'complete' or event == 'append'
        
        handler = self.__handlers__.get((elem, event), None)
        if handler:
            return (await handler(elem, message.value)) if asyncio.iscoroutinefunction(handler)\
                  else handler(elem, message.value)              
            
        handler = getattr(elem, event, False)                                
        if handler:                
            result = (await handler(elem, message.value)) if asyncio.iscoroutinefunction(handler)\
                  else handler(elem, message.value) 
            if query:                        
                result = Answer(event, message, result)                
            return result
        elif event == 'changed':            
            elem.value = message.value                                        
        else:
            error = f"{message.block}/{message.element} doesn't contain '{event}' method type!"
            self.log(error)                     
            return Error(error)
    
    def log(self, str, type = 'error'):        
        scr = self.screen.name if self.screens else 'void'
        str = f"session: {self.session}, screen: {scr}, message: {self.last_message}\n  {str}"
        if type == 'error':
            logging.error(str)
        else:
            logging.warning(str)    

User.type = User
User.last_user = None
User.toolbar = []
User.sessions = {}
User.count = 0

def make_user(request):
    session = f'{request.remote}-{User.count}'
    User.count += 1    
    requested_connect = request.headers.get('session')
    if requested_connect:
        user = User.sessions.get(requested_connect, None)
        if not user:
            error = f'Session id "{requested_connect}" is unknown. Connection refused!'
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
        User.last_user.__handlers__[elem, event] = fn
    return h