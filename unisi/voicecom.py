# Copyright © 2024 UNISI Tech. All rights reserved.
from difflib import SequenceMatcher
from word2number import w2n 
from .users import *
from .units import *
from .tables import Table
from .containers import *

def find_most_similar_sequence(input_string, string_list):    
    best_match = ""
    highest_ratio = 0
    for string in string_list:
        matcher = SequenceMatcher(None, input_string, string.lower())
        ratio = matcher.ratio()
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_match = string

    return best_match, highest_ratio

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
    screen = ['top'], 
    push = ['execute','run']         
)

root_commands = ['select', 'screen', 'stop', 'reset']   

modes = dict( #-> actions    
    text = ['text', 'left', 'right', 'up', 'down','backspace','delete', 'space', 'tab', 'enter'],
    number = ['number', 'backspace','delete'],    
    graph = ['node', 'edge'],
    table = ['page','row', 'column', 'left', 'right', 'up', 'down','backspace','delete'], 
    command = ['push']   
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
        self.block = self.assist_block()    
        self.user = user    
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
        self.calc_interactive_units()
        self.screen = screen
        self.reset()

    @property
    def  context_options(self):
        return self.context_list.options

    @context_options.setter
    def context_options(self, names):
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
                
    def calc_interactive_units(self):
        interactive_names = []
        name2unit = {}        
        self.sreen_name = self.user.screen.name        
        for block in flatten(self.user.screen.blocks):                        
            for elem in flatten(block.value):
                if getattr(elem, 'edit', True):
                    pretty_name = pretty4(elem.name)
                    name2unit[pretty_name] = elem
                    interactive_names.append(pretty_name)  
        interactive_names.sort()                  
        self.unit_names = interactive_names
        self.name2unit = name2unit
    
    def activate_unit(self, unit):
        if self.unit:
            self.unit.active = False
            self.unit.focus = False
        match unit.type:
            case 'string':
                self.mode = 'text'
            case 'range':
                self.mode = 'number'
            case _: 
                self.mode = unit.type
        if self.unit:
            self.unit.active = False
            self.focus = True
        self.unit = unit
        unit.active = True
        self.buffer = []
        commands = modes.get(self.mode, []) + root_commands 
        syn_commands = []
        for command in commands:
            if command in command_synonyms:
                syn_commands.extend(command_synonyms[command])
        if syn_commands:
            commands.extend(syn_commands)
        commands.sort()
        self.commands = commands

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
            case 'reset':
                self.reset()
            case 'stop':
                result = self.stop()
            case _:
                result = self.process_context(word)
        return result
    
    def reset(self):
        self.buffer = []
        self.mode = 'select'
        self.unit = None
        self.input.value = ''
        self.message.value = 'Select an element'
        self.context_list.value = None
        self.commands = root_commands
        self.context = None
        self.context_options = self.unit_names

    def buffer_suits_name(self, word: str):
        self.buffer.append(word)
        name = ' '.join(self.buffer)         
        return find_most_similar_sequence(name, self.context_options)
                    
    def process_context(self, word):
        u = self.unit           
        result = None     
        match self.mode:            
            case 'screen':
                if (word == 'ok' or word == 'okay') and self.context:
                    result = self.user.set_screen(self.context)
                    self.set_screen(self.user.screen)
                    return result
                else:
                    screen_name, similarity = self.buffer_suits_name(word)
                    if similarity > 0.9:
                        return self.user.set_screen(screen_name)
                    else:
                        self.context = screen_name
                        self.message.value = '"Ok" to confirm'
            case 'select':
                if self.unit_names:
                    if word == 'ok' or word == 'okay':
                        self.activate_unit(self.name2unit[self.context])
                    else:
                        unit_name, similarity = self.buffer_suits_name(word)                    
                        if similarity >= 0.8:
                            self.activate_unit(self.name2unit[unit_name])
                        elif unit_name:
                            self.context = unit_name
                            self.message.value = '"Ok" to confirm'
                        else:
                            self.commands = []
                            self.message.value = 'No such unit. Continue..'
                            self.input.value = ' '.join(self.buffer)
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
            case 'command':
                pass
                
        
        

