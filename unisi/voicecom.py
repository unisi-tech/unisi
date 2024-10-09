# Copyright © 2024 UNISI Tech. All rights reserved.
from difflib import get_close_matches, SequenceMatcher
from word2number import w2n 
from .users import *
from .units import *
from .tables import Table
from .containers import *

def similarity(string1, string2):
    return SequenceMatcher(None, string1, string2).ratio()

def word_to_number(sn):
    try: 
        return w2n.word_to_num(sn) 
    except:
        return None
    
command_synonyms = dict( #-> words    
    value = ['is', 'equals'],
    select = ['choose','set'],
    backspace = ['back'],    
    enter = ['push', 'execute','run'],
    clean = ['empty','erase'],
    screen = ['top']          
)

root_commands = ['select', 'screen', 'stop']   

modes = dict( #-> actions    
    text = ['text', 'left', 'right', 'up', 'down','backspace','delete', 'space', 'tab', 'enter'],
    number = ['number', 'backspace','delete'],    
    graph = ['node', 'edge'],
    table = ['page','row', 'column', 'left', 'right', 'up', 'down','backspace','delete'],    
)

word2command = {}
for command, syns in command_synonyms.items():
    for syn in syns:
        word2command[syn] = command        
word2command.update({command: command for command in root_commands})
for mode, commands in modes.items():
    word2command.update({c:c for c in commands})

class VoiceCom:
    def __init__(self, user):
        self.user = user
        self.block = None
        self.set_screen(user.screen)

    def assist_block(self) -> Block:
        self.input = Edit('Input', '')
        self.message = Edit('System message', '', edit = False)
        self.context_list = Select('Elements', type = 'list')
        self.command_list = Select('Commands', type = 'list')
        
        return Block("Voice Assistant", 
            self.input,
            self.message,
            self.context_list,
            self.command_list,
            closable = True, width = 390                                    
        )        
    def set_screen(self, screen):
        self.buffer = []
        self.mode = 'select'
        self.unit = None
        if not self.block:
            self.block = self.assist_block()        
        self.calc_interactive_units()
        self.screen = screen

    @property
    def  unit_names(self):
        return self.context_list.options

    @unit_names.setter
    def unit_names(self, names):
        self.context_list.options = names       

    @property
    def commands(self):
        return self.command_list.options
        
    @commands.setter
    def commands(self, commands):
        self.command_list.options = commands

    @property
    def context(self):
        return self.context_list.value
    
    @context.setter
    def context(self, context):
        self.context_list.value = context
        unit = self.name2unit[context]
        self.activate_unit(unit)
                
    def calc_interactive_units(self):
        interactive_names = []
        name2unit = {}
        self.sreen_name = self.user.screen.name        
        for block in flatten(self.user.screen.blocks):            
            name2unit[block.name] = block            
            for elem in flatten(block.value):
                if getattr(elem, 'edit', True):
                    pretty_name = pretty4(elem.name)
                    name2unit[pretty_name] = elem
                    interactive_names.append(pretty_name)
        self.unit_names = interactive_names
        self.name2unit = name2unit
    
    def activate_unit(self, unit):
        match unit.type:
            case 'string':
                self.mode = 'text'
            case 'range':
                self.mode = 'number'
            case _: 
                self.mode = unit.type
        if self.unit:
            self.unit.active = False
        self.unit = unit
        unit.active = True
        self.buffer = []

    def start(self):
        if self.screen.blocks[-1] != self.block:
            self.screen.blocks.append(self.block)            
    def stop(self):
        if self.screen.blocks[-1] == self.block:
            self.screen.blocks.remove(self.block)            

    def process_word(self, word: str):  
        self.input.value = word
        self.message.value = ''
        result = None
        if word:      
            if self.mode == 'number' or self.mode == 'text':
                #double repeat command word cause execution
                if self.buffer and self.buffer[-1] == word and word in word2command:
                    self.buffer.pop()
                    result = self.exec_command(word)
                else:                    
                    result = self.process_context(word)
            else:
                result = self.exec_command(word)
        return result

    def exec_command(self, word: str):  
        """word is a command or a unit name or a name part"""
        self.message.value = ''            
        command = word2command.get(word, None)
        result = None
        match command:
            case 'select':            
                if unit := self.buffer_suits_name():
                    self.activate_unit(unit)
                    self.buffer = []
                else:
                    result = self.process_context(word)
            case 'screen':
                clist = self.context_list
                clist.options = [getattr(s, 'name')for s in self.user.screens 
                    if hasattr(s, 'name') and s.name != self.user.sreen.name]
                clist.value = None
                self.mode = command
                self.buffer = []
            case 'stop':
                result = self.stop()
            case _:
                result = self.process_context(word)
        return result

    def buffer_suits_name(self, word: str):
        self.buffer.append(word)
        name = ' '.join(self.buffer) 
        recon = get_close_matches(name, self.unit_names, n=1)        
        return  (recon[0],  similarity(name, recon[0]))  if recon else '', 0
                    
    def process_context(self, word):
        u = self.unit           
        result = None     
        match self.mode:            
            case 'screen':
                if word == 'ok' and self.context:
                    result = self.user.set_screen(self.context)
                    self.set_screen(self.user.screen)
                else:
                    screen_name, similarity = self.buffer_suits_name(word)
                    if similarity > 0.9:
                        result = self.user.set_screen(screen_name)
                    else:
                        self.context_list.value = screen_name
                        self.message.value = '"Ok" to confirm'
            case 'select':
                if word == 'ok' and self.context:
                    self.activate_unit(self.name2unit[self.context])
                else:
                    unit_name, similarity = self.buffer_suits_name(word)
                    if similarity > 0.9:
                        self.activate_unit(self.name2unit[unit_name])
                    else:
                        self.command_list.value = unit_name
                        self.message.value = '"Ok" to confirm'
            case 'text':
                match word:
                    case 'left' :
                        if u.x > 0:
                            u.x -= 1
                    case 'right' :
                        if u.x < len(u.value) - 1:
                            u.x += 1                    
                    case 'backspace' :
                        if u.x > 0:
                            u.value = u.value[:u.x - 1] + u.value[u.x:]
                            u.x -= 1
                    case 'delete' :
                        if u.x < len(u.value) - 1:
                            u.value = u.value[:u.x] + u.value[u.x+1:]
                    case 'space' :
                        if hasattr(u, 'x'):
                            u.value = u.value[:u.x] + ' ' + u.value[u.x:]
                            u.x += 1
                    case _ :
                        if u.x < len(u.value):
                            u.value = u.value[:u.x] + word + u.value[u.x:]
                            u.x += len(word)

            case 'number':
                svalue = str(self.value)
                match word:
                    case 'left' :
                        if u.x > 0:
                            u.x -= 1
                    case 'right' :
                        if u.x < len(svalue) - 1:
                            u.x += 1
                    case 'backspace' :
                        if u.x > 0:
                            u.value = float(svalue[:u.x - 1] + svalue[u.x:])
                            u.x -= 1
                    case 'delete' :
                        if u.x < len(svalue) - 1:
                            u.value = float(svalue[:u.x] + svalue[u.x+1:])
                    case _ :
                        num = word_to_number(word)
                        if num is not None and  hasattr(u, 'x') :
                            if num >= 0 and num <= 9:                        
                                u.value = float(svalue + str(num))
                                u.x += len(word)
                        
            case _:
                pass
                
        
        

