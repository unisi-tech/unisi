# Copyright © 2024 UNISI Tech. All rights reserved.
from websocket import create_connection
from enum import IntFlag
import json, requests, os
from .common import *

class Event(IntFlag):
    none = 0
    update = 1
    invalid = 2
    message = 4
    update_message = 5
    progress = 8
    update_progress = 9
    unknown = 16
    unknown_update = 17
    dialog = 32
    screen = 65
    complete = 128
    append = 256    

ws_header = 'ws://'
wss_header = 'wss://'
ws_path = 'ws'

message_types = ['error','warning','info']

class Proxy:
    """UNISI proxy"""
    def __init__(self, host_port, timeout = 7, ssl = False, session = ''):
        addr_port = f'{wss_header if ssl else ws_header}{host_port}'
        addr_port = f'{addr_port}{"" if addr_port.endswith("/") else "/"}{ws_path}'
        self.host_port = f'{"https" if ssl else "http"}://{host_port}'
        if session:
            addr_port = f'{addr_port}?{session}'
        self.conn = create_connection(addr_port, timeout = timeout)
        self.screen = None       
        self.screens = {}
        self.dialog = None
        self.event = None
        self.request(None)

    def close(self):
        self.conn.close()
    
    @property
    def screen_menu(self):
        return [name_icon[0] for name_icon in self.screen['menu']] if self.screen else []
    
    @property
    def commands(self):
        """return command objects"""
        return self.elements(types=['command'])        
            
    def element(self, name, block_name = None):
        """return the element only if 1 element has such name"""
        result = None
        name2block = self.screen['name2block']
        for block in [name2block[block_name]] if block_name else name2block.values(): 
            for el in flatten(block['value']):
                if el['name'] == name:
                    if not result:
                        result = el
                    else:
                        return None
        return result
    
    def elements(self, block = None, types = None):
        """get elements with filtering types and blocks"""
        if block:
            return [el for el in flatten(block['value']) if not types or el['type'] in types]
        answer = []
        for block in self.screen['name2block'].values(): 
            answer.extend([el for el in flatten(block['value']) if not types or el['type'] in types])
        return answer
    
    def block_name(self, element):
        is_name = isinstance(element, str)
        for block in self.screen['name2block'].values():
            for el in flatten(block['value']):
                if el['name'] == element if is_name else el == element:
                    return block['name']
                
    def upload(self, fpath):
        """upload file to the server and get its server path"""
        file = open(fpath, "rb")        
        response = requests.post(self.host_port, files = {os.path.basename(fpath): file})
        return getattr(response, 'text', '')
    
    def command(self, command, value = None):
        return self.interact(self.make_message(command, value))        
        
    def command_upload(self, command, fpath):
        """upload file to the server and call command"""    
        spath = os.path.abspath(fpath) if 'localhost' in self.host_port else self.upload(fpath)
        return self.command(command, spath) if spath else Event.invalid                    
    
    def make_message(self, element, value = None, event = 'changed'):
        if isinstance(element, str):
            element = self.element(element)            
        if event != 'changed' and event not in element:
            return None        
        return ArgObject(block = self.block_name(element), element = element['name'], 
            event = event, value = value)
    
    def interact(self, message, progress_callback = None):
        """progress_callback is def (proxy:Proxy)"""        
        while self.request(message) & Event.progress:
            if progress_callback:
                progress_callback(self)
            message = None
        return self.event
    
    def request(self, message):
        """send message or message list, get responce, return the responce type"""
        if message:
            self.conn.send(toJson(message))
        responce = self.conn.recv()
        message = json.loads(responce) 
        return self.process(message) 
    
    def set_value(self, element, new_value):
        if isinstance(element, str):
            element = self.element(element)            
        element['value'] = new_value
        ms = self.make_message(element, new_value)
        return self.interact(ms) if ms else Event.invalid
    
    def set_screen(self, name):
        screen = self.screens.get(name)
        if not screen:
            if name in self.screen_menu:
                mtype = self.request(ArgObject(block = 'root', element = None, value = name))
                return mtype == Event.screen 
            else:
                return False
        return True 
    
    @property
    def dialog_commands(self):
        return self.dialog['commands'] if self.dialog else []
    
    def dialog_responce(self, command: str | None):
        if not self.dialog:
            self.event = Event.invalid
            return  self.event
        return self.interact(ArgObject(block = self.dialog['name'], value = command))
       
    def process(self, message):        
        self.message = message   
        if not message:
            self.event = Event.none 
            self.mtype = None
        else:    
            mtype = message.get('type')        
            self.mtype = mtype
            if mtype == 'screen':
                self.screen = message
                self.screens[self.screen['name']] = message
                name2block = {block['name']: block for block in flatten(message['blocks'])}            
                name2block['toolbar'] = {'name': 'toolbar', 'value': message['toolbar']}
                message['name2block'] = name2block
                self.event = Event.screen
            elif mtype == 'dialog':
                self.dialog = message
                self.event = Event.dialog
            elif mtype == 'complete':
                return Event.complete
            elif mtype == 'append':
                self.event = Event.append
            elif mtype == 'update':
                self.update(message)
                self.event = Event.update
            else:
                updates = message.get('updates')
                if updates:
                    self.update(message)                                        
                if type in message_types:
                    self.event = Event.update_message if updates else Event.message 
                if type == 'progress':
                    self.event = Event.update_progress if updates else Event.progress
                else:
                    self.event = Event.unknown_update if updates else Event.unknown
        return self.event
        
    def update(self, message):
        """update screen from the message"""
        result = Event.update
        updates = message.updates
        for update in updates:
            path = update['path']
            name2block = self.screen['name2block']
            if len(path) == 1: #block
                name2block[block] = update['data']
            else:
                block, element = path
                for el in flatten(name2block[block]['value']):
                    if el['name'] == element:
                       el.__dict__ =  update['data'].__dict__ 
                       break
                else:
                    result = Event.unknown_update
        return result

    
