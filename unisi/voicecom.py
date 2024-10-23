# Copyright © 2024 UNISI Tech. All rights reserved.
from difflib import SequenceMatcher
from word2number import w2n 
from .users import *
from .units import *
from .containers import Block

def find_most_similar_sequence(input_string, string_list):    
    best_match = ""
    highest_ratio = 0
    for string in string_list:
        matcher = SequenceMatcher(None, input_string.lower(), string.lower())
        ratio = matcher.ratio()
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_match = string
    return best_match, highest_ratio

def word_to_number(word: str):
    sn = word.replace(',', '')
    try: 
        return float(sn) 
    except:
        try: 
            return w2n.word_to_num(sn) 
        except:
            return None
    
command_synonyms = dict( #-> words    
    value = ['is', 'equals'],
    root = ['select', 'choose','set'],
    backspace = ['back'],    
    enter = ['push', 'execute','run'],
    clean = ['empty','erase'],
    screen = ['menu'], 
    push = ['execute','run'],         
    reset = ['cancel'],
    ok = ['okay']
)

root_commands = ['root', 'screen', 'stop', 'reset', 'ok']   
ext_root_commands = root_commands[:]

modes = dict( #-> actions    
    text = ['text', 'left', 'right', 'up', 'down','backspace','delete', 'space', 'tab', 'enter', 'undo','clean'],
    number = ['number', 'backspace','delete', 'undo','clean'],    
    graph = ['node', 'edge'],
    table = ['page','row', 'column', 'left', 'right', 'up', 'down','backspace','delete'], 
    command = ['push']   
)
word2command = {}
for command, syns in command_synonyms.items():
    for syn in syns:
        word2command[syn] = command          
    if command in root_commands:
        ext_root_commands.extend(syns)

word2command.update({command: command for command in root_commands})
for mode, commands in modes.items():
    word2command.update({c:c for c in commands})
    

class VoiceCom:
    def __init__(self, user):
        self.block = self.assist_block(user)    
        self.user = user    
        self.previous_unit_value_x = None
        self.unit = None
        self.cached_commands = {}
        self.set_screen(user.screen)

    async def keyboard_input(self, _, value):
        return await self.process_string(value)

    def assist_block(self, user) -> Block:
        self.input = Edit('Recognized words', '', update = self.keyboard_input)
        self.message = Edit('System message', '', edit = False)
        self.context_list = Select('Elements', None, self.select_elem, type = 'list', width = 250)
        self.command_list = Select('Commands', None, self.select_command, type = 'list', width = 250)
        
        block = Block("Mate:", 
            self.message,
            self.input,            
            self.context_list,
            self.command_list,
            closable = True, icon = 'mic'                                
        )      
        block.set_reactivity(user)  
        return block
    
    def set_screen(self, screen):        
        self.calc_interactive_units()
        self.screen = screen
        self.reset()

    def select_elem(self, elem, value):
        elem.value = value
        if value:
            if self.mode == 'screen':
                self.user.set_screen(value)
            self.activate_unit(self.name2unit.get(value, None))    

    async def select_command(self, _, value):
        _.value = None        
        return await self.process_word(value)        

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
        
    def set_mode(self, mode):
        self.context = None
        self.mode = mode
        self.buffer = []
        self.previous_unit_value_x = None
        if not (commands := self.cached_commands.get(mode, None)):            
            syn_commands = []
            commands = modes.get(mode, []) + root_commands
            for command in commands:
                if command in command_synonyms:
                    syn_commands.extend(command_synonyms[command])        
            commands.extend(syn_commands)        
            commands.sort()
            self.cached_commands[mode] = commands
        self.commands = commands
        self.input.value = mode
        self.message.value = 'Continue..'
        match self.mode:
            case 'switch' | 'check':
                self.context_options = ['true', 'false','yes', 'no', 'on', 'off']
            case 'select' | 'list' | 'radio':
                self.context_options = self.unit.options
            case 'tree':
                self.context_options = list(self.unit.options)  
            case 'screen':
                self.context_options = [getattr(s, 'name')for s in self.user.screens 
                    if hasattr(s, 'name') and s.name != self.user.screen.name]
                self.message.value = 'Select a screen'          
            case _:
                self.context_list.options = []
        self.context = None        
    
    def activate_unit(self, unit: Unit | None):
        if self.unit:
            self.unit.active = False
            self.unit.focus = False
        self.unit = unit
        self.message.value =  'Select a command'
        if unit:
            match unit.type:
                case 'string':
                    mode = 'text'
                case 'range':
                    mode = 'number'
                case _: 
                    mode = unit.type        
            unit.active = True
            unit.focus = True            
            self.set_mode(mode)
            if unit.type == 'text' or unit.type == 'number':
                self.previous_unit_value_x = unit.value, unit.x
        else:
             self.commands = ext_root_commands                    

    def start(self):
        if self.screen.blocks[-1] != self.block:
            self.screen.blocks.append(self.block)    
        self.reset()        
    def stop(self):
        if self.screen.blocks[-1] == self.block:
            self.screen.blocks.remove(self.block)            

    async def process_string(self, string: str) -> any:  
        screen_changed = None
        for word in string.split(' '):
            if word:
                result = await self.process_word(word)
                if not screen_changed:
                    screen_changed = result
        return screen_changed 

    async def process_word(self, word: str) -> any:  
        self.input.value = word
        self.message.value = ''
        if word:      
            command = word2command.get(word, None)
            match self.mode:
                case 'number'|'text':
                #double repeat command word cause execution
                    if self.mode == 'number':
                        num = word_to_number(word)
                        if command:
                            return await self.run_command(command)
                        elif num is not None:
                            self.previous_unit_value_x = self.unit.value, self.unit.x                            
                            self.unit.value = num                                                    
                        else:
                            self.message.value = 'Not a number'
                    elif self.mode == 'text':
                        if self.buffer and self.buffer[-1] == word and command:
                            self.buffer.pop()
                            if self.previous_unit_value_x:
                                self.unit.value, self.unit.x = self.previous_unit_value_x                            
                                self.previous_unit_value_x = None
                            return await self.run_command(command)
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
                case 'switch' | 'check' | 'select' | 'list' |'radio' | 'tree':
                    if command:
                        return await self.run_command(command)
                    if self.context:                        
                        self.message.value = ''
                        self.unit.value = self.context in ('true', 'yes', 'on') if self.mode == 'switch' else self.context
                    else:
                        choice, similarity = self.buffer_suits_name(word)                    
                        if similarity >= 0.8:
                            self.unit.value = choice in ('true', 'yes', 'on') if self.mode == 'switch' else choice
                            self.message.value = ''                                
                        elif choice:
                            self.context = choice
                            self.message.value = '"Ok" to confirm'
                        else:
                            self.commands = []
                            self.message.value = 'Continue..'
                            self.buffer = []
                            self.input.value = ''                    
                case 'root': 
                    if command == 'ok' and self.context:
                        self.activate_unit(self.name2unit[self.context])
                    elif command:
                        return await self.run_command(command)
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
                    else:
                        screen_name, similarity = self.buffer_suits_name(word)
                        if similarity > 0.9:
                            self.user.set_screen(screen_name)
                        else:
                            self.context = screen_name
                            self.message.value = '"Ok" to confirm'             
                case _:
                    if command: 
                        return await self.run_command(command)  
                    else:       
                        self.message.value = 'Unknown command.'

    async def run_command(self, command: str):  
        self.message.value = ''                            
        match command:
            case 'root':            
                self.reset()                
            case 'screen':                
                self.set_mode('screen')                                
            case 'reset':
                self.reset()
            case 'stop':
                self.stop()
            case _:
                if self.unit:
                    return await self.context_command(command)
                else:
                    self.message.value = 'Command is out of context.'

    def reset(self):
        self.buffer = []
        self.mode = 'root'
        if dialog := self.user.active_dialog:
            commands = ext_root_commands + ['close']
            commands.sort
            self.commands = commands
            options = [u.name for u in flatten(dialog.value)]
            options.sort()
            self.context_options = options            
        else:
            self.commands = ext_root_commands
            self.context_options = self.unit_names
        if self.unit:
            self.unit.active = False
            self.unit.focus = False
            self.unit = None
        self.input.value = ''
        self.message.value = 'Select a command or element'
        self.context = None        

    def buffer_suits_name(self, word: str):
        self.buffer.append(word)
        name = ' '.join(self.buffer)         
        return find_most_similar_sequence(name, self.context_options)
                    
    async def context_command(self, command: str):
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
                        if self.previous_unit_value_x:
                            u.value, u.x = self.previous_unit_value_x 
                    case 'clean':
                        u.value = ''
                    case _ :
                        self.message.value = 'Command is ouside context'
            case 'number':
                svalue = str(u.value)
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
                    case 'clean':
                        u.value = None
                    case _ : 
                        self.message.value = 'Command is ouside context'
                                                
            case 'command':
                if command == 'ok' or command == 'push':
                    handler = self.unit.changed
                    if handler:
                        return await call_anysync(handler, self.unit, None)                  
                self.message.value = 'Command is ouside context'                                    