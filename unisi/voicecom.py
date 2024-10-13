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
    screen = ['menu'], 
    push = ['execute','run'],         
    reset = ['cancel'],
    ok = ['okay']
)

root_commands = ['select', 'screen', 'stop', 'reset', 'ok']   

modes = dict( #-> actions    
    text = ['text', 'left', 'right', 'up', 'down','backspace','delete', 'space', 'tab', 'enter', 'undo'],
    number = ['number', 'backspace','delete', 'undo'],    
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
        self.previous_unit_value_x = None
        self.unit = None
        self.set_screen(user.screen)

    def assist_block(self) -> Block:
        self.input = Edit('Recognized words', '', update = lambda _,value: self.process_word(value))
        self.message = Edit('System message', '', edit = False)
        self.context_list = Select('Elements', None, self.select_elem, type = 'list')
        self.command_list = Select('Commands', None, self.select_command, type = 'list')
        
        return Block("Mate:", 
            self.message,
            self.input,            
            self.context_list,
            self.command_list,
            closable = True, width = 390, icon = 'mic'                                    
        )        
    def set_screen(self, screen):        
        self.calc_interactive_units()
        self.screen = screen
        self.reset()

    def select_elem(self, elem, value):
        self.activate_unit(self.name2unit[value])    

    def select_command(self, elem, value):
        if command := word2command.get(value, None):
            self.run_command(command)
        self.input.value = value
        self.message.value = command if command else ''
        self.command_list.value = None

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
        
        self.unit = unit
        unit.active = True
        unit.focus = True
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
        if unit.type == 'text' or unit.type == 'number':
            self.previous_unit_value_x = unit.value, unit.x
        self.message.value = ''

    def start(self):
        if self.screen.blocks[-1] != self.block:
            self.screen.blocks.append(self.block)            
    def stop(self):
        if self.screen.blocks[-1] == self.block:
            self.screen.blocks.remove(self.block)            

    def process_word(self, word: str):  
        self.input.value = word
        self.message.value = ''
        if word:      
            command = word2command.get(word, None)
            match self.mode:
                case 'number'|'text':
                #double repeat command word cause execution
                    if self.mode == 'number' and not command:
                        num = word_to_number(word)
                        if num is not None:
                            self.previous_unit_value_x = self.unit.value, self.unit.x
                            if self.unit.x == -1:
                                self.unit.value = num
                            elif num >= 0 and num <= 9:            
                                snum = str(num)                                                
                                svalue = str(self.unit.value)
                                self.unit.value = float(svalue[:self.unit.x] + snum + svalue[self.unit.x:])
                                self.unit.x += 1
                        else:
                            self.message.value = 'Not a number'
                    elif self.mode == 'text':
                        if self.buffer and self.buffer[-1] == word and command:
                            self.buffer.pop()
                            self.run_command(command)
                        else:
                            self.previous_unit_value_x = self.unit.value, self.unit.x
                            self.buffer = [word]
                            value = self.unit.value
                            if self.unit.x == -1:
                                if value:
                                    self.unit.value += ' ' + word
                                else:
                                    self.unit.value = word                                                    
                                self.unit.x = len(self.unit.value)
                            else:                 
                                word += ' '           
                                self.unit.value = value[:self.unit.x] + word + value[self.unit.x:]
                                self.unit.x += len(word)
                case 'select': 
                    if command == 'ok' and self.context:
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
                            self.message.value = 'Continue..'
                            self.input.value = ' '.join(self.buffer)                      
                case 'screen':
                    if command == 'ok' and self.context:
                        self.user.set_screen(self.context)                        
                        return self.set_screen(self.user.screen)
                    else:
                        screen_name, similarity = self.buffer_suits_name(word)
                        if similarity > 0.9:
                            return self.user.set_screen(screen_name)
                        else:
                            self.context = screen_name
                            self.message.value = '"Ok" to confirm'             
                case _:
                    if command: 
                        self.run_command(command)  
                    else:       
                        self.message.value = 'Unknown command.'

    def run_command(self, command: str):  
        self.message.value = ''                            
        match command:
            case 'select':            
                self.mode = 'select'
                self.buffer = []
                self.commands = root_commands                    
                self.message.value = 'Say an element or command'
            case 'screen':
                self.context_options = [getattr(s, 'name')for s in self.user.screens 
                    if hasattr(s, 'name') and s.name != self.user.sreen.name]
                self.context = None
                self.mode = command
                self.buffer = []
                self.message.value = 'Select a screen'
            case 'reset':
                self.reset()
            case 'stop':
                self.stop()
            case _:
                if self.unit:
                    self.context_command(command)
                else:
                    self.message.value = 'Command is out of context.'
    def reset(self):
        self.buffer = []
        self.mode = 'select'
        if self.unit:
            self.unit.active = False
            self.unit.focus = False
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
                    
    def context_command(self, command: str):
        u = self.unit                   
        match self.mode:                        
            case 'text':
                match command:
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
                    case 'undo':
                        u.value, u.x = self.previous_unit_value_x 
                    case _ :
                        self.message.value = 'Command is ouside context'
            case 'number':
                svalue = str(self.value)
                match command:
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
                        self.message.value = 'Command is ouside context'
                        
            case 'command':
                if command == 'ok' or command == 'push':
                    return self.unit.accept(None)
                else:
                    self.message.value = 'Command is ouside context'                                    