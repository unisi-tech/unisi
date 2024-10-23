# Copyright © 2024 UNISI Tech. All rights reserved.
from .utils import *
from .units import *
from .common import *
from .voicecom import VoiceCom
from .containers import Dialog, Screen
from .multimon import notify_monitor, logging_lock, run_external_process
from .dbunits import dbshare, dbupdates
import sys, asyncio, logging, importlib

class User:          
    last_user = None
    toolbar = []    
    count = 0
    
    def __init__(self, session: str, share = None):          
        self.session = session        
        self.active_dialog = None        
        self.last_message = None                               
        self.changed_units = set()
        self.voice = None

        if share:            
            self.screens = share.screens            
            self.screen_module = share.screen_module if share.screens else []
            self.handlers =  share.handlers        
            
            if share.reflections:            
                share.reflections.append(self)
            else:
                share.reflections = [share, self]                    
            self.reflections = share.reflections
        else:
            self.screens = []        
            self.reflections = []
            self.screen_module = None         
            self.handlers = {} 

        User.last_user = self
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
        if self.reflections and not message.screen_type:                        
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
        if not isinstance(screen.blocks, list):
            screen.blocks = [screen.blocks]
        if screen.toolbar:
            screen.toolbar += User.toolbar
        else: 
            screen.toolbar = User.toolbar  
        screen.set_reactivity(self)        
        module.screen = screen#ChangedProxy(screen, screen)                                 
        return module
    
    async def delete(self):
        uss = Unishare.sessions
        if uss and uss.get(self.session):
            del uss[self.session]        
        if self.reflections: #reflections is common array
            if len(self.reflections) == 2: 
                self.reflections.clear() #1 element in user.reflections has no sense
            else:
                self.reflections.remove(self)  
        if notify_monitor:
            await notify_monitor('-', self.session, self.last_message)   
        if config.share:
            self.log(f'User is disconnected, session: {self.session}', type = 'info')            

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
            self.screen_module = main
            if hasattr(main, 'prepare'):  
                main.prepare()            
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
        return self.screen_process(ArgObject(block = 'root', element = None, value = name))
    
    async def result4message(self, message):
        result = None        
        self.last_message = message     
        if dialog := self.active_dialog:            
            if message.element is None: #dialog command button is pressed
                self.active_dialog = None    
                if self.reflections:            
                    await self.broadcast(close_message)                                    
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

    async def eval_handler(self, handler, *params):
        if notify_monitor:
            await notify_monitor('+', self.session, self.last_message)        
        result = await call_anysync(handler, *params)
        if notify_monitor:
            await notify_monitor('-', self.session, None)        
        return result
    
    def register_changed_unit(self, unit, property = None, value = None):
        """add unit to changed_units if it is changed outside of message"""
        if property == 'value':
            property = 'changed'
        m = self.last_message
        if not m or unit.name != m.element or property != m.event or value != m.value:
            self.changed_units.add(unit)            

    @property
    def blocks(self):
        return [self.active_dialog, *self.screen.blocks] if self.active_dialog and \
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
                    if not elname:
                        return bl
                    for c in flatten(bl.value):
                        if c.name == elname:
                            return c
        
    def find_path(self, elem) -> list:        
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
        reload_screen = any(u.type == 'screen' for u in self.changed_units)
        if reload_screen or raw is True or raw == Redesign:            
            self.screen.reload = reload_screen or raw == Redesign                              
            raw = self.screen
        else:
            match raw:
                case None: 
                    if self.changed_units:
                        raw = Message(*self.changed_units, user = self) 
                case Message():
                    if self.changed_units:
                        message_units = [x['data'] for x in raw.updates]
                        self.changed_units.update(message_units)
                        raw.set_updates(self.changed_units)
                    raw.fill_paths4(self)                                    
                case Unit():
                    self.changed_units.add(raw)
                    raw = Message(*self.changed_units, user = self) 
                case list() | tuple(): #raw is *unit
                    self.changed_units.update(raw)
                    raw = Message(*self.changed_units, user = self) 
                case _: ...
                    
        self.changed_units.clear()           
        return raw
    
    def screen_process(self, message):
        screen_change_message = message.screen and self.screen.name != message.screen
        if screen_change_message or message.screen_type:
            for s in self.screens:
                if s.name == message.value:
                    if self.screen_module != s:
                        self.changed_units.add(s.screen)
                        self.screen_module = s   
                        if screen_change_message:
                            break                    
                        if self.voice:
                            self.voice.set_screen(s.screen)       
                            self.voice.start()                              
                        if getattr(s.screen,'prepare', None):
                            s.screen.prepare()
                        return True 
            else:        
                error = f'Unknown screen name: {message.value}'   
                self.log(error)
                return Error(error)

    async def process(self, message):        
        if screen_result := self.screen_process(message):
            return screen_result
        elif message.voice_type:            
            if not self.voice:
                self.voice = VoiceCom(self)                
            if message.event == 'listen':                
                if message.value:
                    self.voice.start()  
                else:
                    self.voice.stop()
            else:
                return await self.voice.process_string(message.value)            
        else:        
            elem = self.find_element(message)          
            if elem:                          
                return await self.process_element(elem, message)              
            error = f'Element {message.block}/{message.element} does not exist!'
            self.log(error)
            return Error(error)        
    async def process_element(self, elem, message):                
        event = message.event         
        handler = self.handlers.get((elem, event), None)
        if handler:
            return await self.eval_handler(handler, elem, message.value)                                                                    
        if hasattr(elem, event):                
            attr = getattr(elem, event)
            if is_callable(attr):
                result = await self.eval_handler(attr, elem, message.value)
                if event in ('complete', 'append', 'get'):                        
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
        
    def monitor(self, session, share = None):
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

    def init_user():
        """make initial user for autotest and evaluating dbsharing"""
        user = User.type(testdir)
        user.load()    
        #register shared db map once
        user.calc_dbsharing()
        return user

    def calc_dbsharing(self):
        """calc connections db and units"""
        dbshare.clear()
        for module in self.screens:
            screen = module.screen
            for block in flatten(screen.blocks):
                for elem in flatten(block.value):
                    if hasattr(elem, 'id'):
                        dbshare[elem.id][screen.name].append({'element': elem.name, 'block': block.name})                                

    async def sync_dbupdates(self):
        sync_calls = []
        for id, updates in dbupdates.items():
            for update in updates:
                if update:
                    screen2el_bl = dbshare[id]
                    exclude = update.get('exclude', False)
                    for user in Unishare.sessions.values():                
                        if not exclude or user is not self:
                            scr_name = user.screen.name
                            if scr_name in screen2el_bl:
                                for elem_block in screen2el_bl[scr_name]: 
                                    update4user = {**update, **elem_block}
                                    sync_calls.append(user.send(update4user))
        dbupdates.clear()
        await asyncio.gather(*sync_calls)
