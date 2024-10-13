# Copyright © 2024 UNISI Tech. All rights reserved.
from .units import *
from .common import pretty4, flatten, delete_unit
from numbers import Number
        
class Block(Unit):    
    def __init__(self, name, *elems, **options):    
        self._mark_changed = None        
        self.name = name        
        self.type = 'block'
        self.value = list(elems)        
        self.add(options)  
        if getattr(self,'scaler', False):
            scaler = ContentScaler(elements = lambda: self.scroll_list)
            self.scaler = scaler
            if not self.value:
                self.value = [[scaler]]
            elif isinstance(self.value[0], list):
                self.value[0].append(scaler)
            else:
                self.value[0] = [self.value, scaler]     

        for elem in flatten(self.value):                        
            if hasattr(elem, 'llm'): 
                if elem.llm is True:
                   dependencies = [obj for obj in flatten(self.value) if elem is not obj and obj.type != 'command'] 
                   exactly = False                               
                elif isinstance(elem.llm, list | tuple):
                    dependencies = elem.llm                    
                    exactly = True
                elif isinstance(elem.llm, Unit):                    
                    dependencies = [elem.llm]
                    exactly = True
                elif isinstance(elem.llm, dict):
                    if elem.type != 'table':                        
                        raise AttributeError(f'{elem.name} llm parameter is a dictionary only for tables, not for {elem.type}!')                                                                
                    elem._llm_dependencies = {fld: (deps if isinstance(deps, list | bool) else [deps]) for fld, deps in elem.llm.items()} 
                    elem.llm = True
                    continue
                else:
                    raise AttributeError(f'Invalid llm parameter value for {elem.name} {elem.type}!')
                if dependencies:
                    elem.llm = exactly                
                    for dependency in dependencies:
                        dependency.add_changed_handler(elem.emit)    
                    elem._llm_dependencies = dependencies
                else:
                    elem.llm = None
                    print(f'Empty dependency list for llm calculation for {elem.name} {elem.type}!')

        user = Unishare.context_user()
        if hasattr(self,'closable'):        
            def close(*_):
                delete_unit(user.screen.blocks, self.name)                
            self.close = close

        self.set_reactivity(user)

    def set_reactivity(self, user, override = False):
        if user:            
            super().set_reactivity(user, override)
            for elem in flatten(self.value):
                elem.set_reactivity(user)
                
    @property
    def compact_view(self) -> str:
        return ','.join(obj.compact_view for obj in flatten(self.value) if obj.value)

    @property
    def scroll_list(self):            
        return self.value[1] if len(self.value) > 1 and isinstance(self.value[1], (list, tuple)) else []
    
    @scroll_list.setter
    def scroll_list(self, lst):
        self.value = [self.value[0] if self.value else [], lst]
        if hasattr(self,'scaler'):
            sval = self.scaler.value
            if sval != 1:
                self.scaler.value = 1
                self.scaler.changed(self.scaler, sval)        
        self.set_reactivity(Unishare.context_user())

    def find(self, elem: Unit | str):
        for e in flatten(self.value):
            if e == elem or e.name == elem:
                return e

class ParamBlock(Block):
    def __init__(self, name, *args, row = 3, **params):
        """ does not need reactivity so Block init is not used"""
        self._mark_changed = None
        if not args:
            args = [[]]
        self._mark_changed = None        
        self.name = name        
        self.type = 'block'
        self.value = list(args)
        self.name2elem = {}
        cnt = 0        

        for param, val in params.items():                    
            pretty_name = pretty4(param)            
            match val:
                case True | False:
                    el = Switch(pretty_name, val)
                case str() | int() | float():
                    el = Edit(pretty_name, val)                
                case tuple() | list():
                    if len(val) != 2:
                        raise ValueError('Composite value has to contain the current value and options value!')
                    options = val[1]
                    if not isinstance(options, list | tuple | dict):
                        raise ValueError('Options value (the second parameter) has to be a list or tuple!')
                    if len(options) == 3 and all(map(lambda e: isinstance(e, Number), options)):
                        el = Range(pretty_name, val[0], options = options)
                    elif isinstance(options, list | tuple):
                        el = Select(pretty_name, val[0], options = options, type = 'select')
                    else: 
                        el = Tree(pretty_name, val[0], options = options)
                case _:
                    raise ValueError(f'The {param} value {val} is not supported. Look at ParamBlock documentation!')
                            
            self.name2elem[param] = el
            
            if cnt % row == 0:
                block = []
                self.value.append(block)
            cnt += 1
            block.append(el)
    @property
    def params(self):
        return {name: el.value for name, el in self.name2elem.items()}

class Dialog:  
    def __init__(self, question, callback, *content, commands = ['Ok','Cancel'],
            icon = 'not_listed_location'):        
        self.type = 'dialog'         
        self.name = question
        self.changed = callback          
        self.commands = commands
        self.icon = icon
        self.value = [[], *content] if content else []        

class Screen(Unit):
    def __init__(self, name):
        self._mark_changed = None        
        self.name = name                
        self.type = 'screen'                                  

