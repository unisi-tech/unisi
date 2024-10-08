from difflib import get_close_matches
from word2number import w2n 
from .users import *
from .units import *
from .containers import *

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

word2command = {v:k for k,v in command_synonyms.items()}
word2command.update({command: command for command in root_commands})
for mode, commands in modes.items():
    word2command.update({c:c for c in commands})

select_str = 'Select element ..'
screen_str = 'Screen ..'
class VoiceCom:
    def __init__(self, user):
        self.user = user
        self.block = None
        self.set_screen(user.screen)

    def set_screen(self, screen):
        self.buffer = []
        self.mode = 'none'
        self.unit = None
        self.calc_interactive_units()
        self.screen = screen
        if not self.block:
            self.block = self.assist_block()        
                
    def calc_interactive_units(self):
        interactive_names = []
        name2unit = {}
        self.sreen_name = self.user.screen.name        
        for block in flatten(self.user.screen.blocks):            
            name2unit[block.name] = block            
            for elem in flatten(block.value):
                if getattr(elem, 'edit', True):
                    name2unit[elem.name] = elem
                    interactive_names.append(elem)
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
        self.unit = unit
        self.buffer = []

    def start(self):
        if self.screen.blocks[-1] != self.block:
            self.screen.blocks.append(self.block)
            return Redesign
    def stop(self):
        if self.screen.blocks[-1] is self.block:
            self.screen.blocks.remove(self.block)
            return Redesign

    def assist_block(self) -> Block:
        self.input = Edit('Input', '')
        self.message = Text('System message')
        self.context_state = Select('Context', value = select_str, options = self.unit_names)
        self.commands = Select('Commands', value = select_str, options = [select_str])
        
        return Block("Voice Assistant", 
            self.input,
            self.message,
            self.context_state,
            closable = True, width = 390                                    
        )        

    def input_word(self, word: str):  
        self.input.value = word
        if word:      
            if self.mode == 'number' or self.mode == 'text':
                #double repeat command word cause execution
                if self.buffer and self.buffer[-1] == word and word in word2command:
                    self.buffer.pop(-1)
                    self.exec_command(word)
                else:                    
                    self.process(word)
            else:
                self.exec_command(word)

    def exec_command(self, word: str):  
        self.message.value = ''            
        command = word2command.get(word)
        match command:
            case  'select':
                if unit := self.buffer_suits_name():
                    self.activate_unit(unit)
                else:
                    self.process()
            case _:
                self.process()        

    def buffer_suits_name(self):
        name = ' '.join(self.buffer) 
        recon = get_close_matches(name, self.unit_names, n=1)
        if recon:
           self.context_state 
            
    def process(self):
        u = self.unit        
        word = self.buffer[0]
        match self.mode:
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
                
        
        

