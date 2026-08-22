# Copyright © 2024 UNISI Tech. All rights reserved.
from .units import *
from .common import pretty4, flatten, delete_unit, close_message
from numbers import Number
        
class Block(Unit):    
    def __init__(self, name, *elems, **options):    
        self._mark_changed = None        
        self.name = name        
        self.type = 'block'
        self.value = list(elems)       
        self._user = None 
        self.add(options)  
        if getattr(self,'scaler', False):
            scaler = ContentScaler(elements = lambda: self.scroll_list)
            self.scaler = scaler
            if not self.value:
                self.value = [[scaler]]
            elif isinstance(self.value[0], list):
                self.value[0].append(scaler)
            else:
                self.value[0] = [self.value[0], scaler]     

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
        
        if hasattr(self,'closable'):        
            def close(*_):
                user = self._user if self._user else  Unishare.context_user()
                blocks = user.screen.blocks
                if isinstance(blocks, ChangedProxy):
                    blocks = blocks._obj
                found, updated = delete_unit(blocks, self.name)
                if found:
                    user.screen.blocks = updated
            self.close = close      

    def __setattr__(self, name, value):      
        super().__setattr__(name, value)
        if name == 'value' and getattr(self, '_user', None):
            self.set_reactivity(self._user)  

    def set_reactivity(self, user, override = False):
        self._user = user
        if user:            
            super().set_reactivity(user, override)
            for elem in flatten(self.value):
                elem.set_reactivity(user)

    @property
    def params(self) -> dict:
        return {el.name : el.value for el in flatten(self.value)}
                
    @property
    def compact_view(self) -> str:
        return ','.join(obj.compact_view for obj in flatten(self.value) if obj.value)

    @property
    def scroll_list(self):            
        return (self.value[1] if len(self.value) > 1 and isinstance(self.value[1], (list, tuple)) else [])\
            if self.scroll else []
    
    @scroll_list.setter   
    def scroll_list(self, lst):
        self.value = ChangedProxy([self.value[0] if self.value else [], lst], self)
        self.scroll = True
        if hasattr(self,'scaler'):
            sval = self.scaler.value
            if sval != 1:
                self.scaler.value = 1
                self.scaler.changed(self.scaler, sval)                  
        for image in lst:
            image.set_reactivity(self._user)      

    def find(self, elem: Unit | str):
        for e in flatten(self.value):
            if e == elem or e.name == elem:
                return e

class ParamBlock(Block):
    def __init__(self, name, /, *args, changed = None, row = 3, strict = 'recurse', persist = False, **params):
        """strict == 'recurse' means to recurse into dict values as an embedded ParamBlock.
        persist, like on any Block/Unit, can be a zero-arg function returning a tuple of
        defining values. Set here, it is a convenience default: each generated field is
        still persisted individually (by its own stable, name-based path), not the block
        as a whole — see UserPersistMixin.sync_keyed_persist / _effective_persist_key_fn."""
        self._mark_changed = None
        if not args:
            args = [[]]        
        self.name = name        
        self.type = 'block'
        self._init_value = list(args)        
        self._row = row
        self._strict = strict
        self._name2elem = {}
        self.value = self._init_value[:]
        self.changed = changed
        self.persist = persist
        self.params = params
                
    @property
    def params(self) -> dict:        
        return {name: el.params for name, el in self._name2elem.items()}
    
    @params.setter   
    def params(self, params: dict):
        self.value = self._init_value[:]
        self._name2elem = {}
        cnt = 0        
        for param, val in params.items():                    
            pretty_name = pretty4(param)            
            match val:
                case True | False:
                    el = Switch(pretty_name, val, self.changed)
                case str() | int() | float():
                    el = Edit(pretty_name, val, self.changed)
                case tuple() | list():
                    if len(val) != 2 or isinstance(val[0], dict):
                        continue
                        #raise ValueError('Composite value has to contain the current value and options value!')
                    options = val[1]
                    if not isinstance(options, list | tuple | dict):
                        raise ValueError('Options value (the second parameter) has to be a list or tuple!')
                    if len(options) == 3 and all(map(lambda e: isinstance(e, Number), options)):
                        el = Range(pretty_name, val[0], self.changed, options = options)
                    elif isinstance(options, list | tuple):
                        el = Select(pretty_name, val[0], self.changed, options = options, type = 'select')
                    else: 
                        el = Tree(pretty_name, val[0], self.changed, options = options)
                case _:
                    if self._strict == 'recurse' and isinstance(val, dict):
                        pb = ParamBlock(pretty_name, changed = self.changed, strict = self._strict, row = self._row, **val)
                        self.value.append(pb)
                        self._name2elem[param] = pb
                        cnt = 0                        
                    elif self._strict:
                        raise ValueError(f'The {param} value {val} is not supported. Look at ParamBlock documentation!')                    
                    continue
                    
            self._name2elem[param] = el
            if cnt % self._row == 0:
                block = []
                self.value.append(block)
            cnt += 1
            block.append(el)

        # Elements above were appended straight into the underlying list (ChangedProxy.append
        # marks *this* block changed but never touches the new children). If the block is
        # already live — params reassigned after the initial screen build, not during __init__ —
        # bring freshly (re)built children up to date: activate reactivity so their own edits
        # are tracked, and register their tree position so persistence (and anything else keyed
        # by unit path) can resolve them. Mirrors what scroll_list.setter already does above.
        user = getattr(self, '_user', None)
        if user:
            for elem in flatten(self.value):
                elem.set_reactivity(user)
            screen = getattr(user, 'screen', None)
            parents = getattr(screen, '_parents', None)
            if parents is not None:
                from .utils import fill_parents  # local import: utils imports Screen from this module
                fill_parents(self.value, self, parents)

class Dialog:  
    def __init__(self, question, callback, *content, commands = ['Ok','Cancel'],
            icon = 'not_listed_location'):        
        self.type = 'dialog'         
        self.name = question
        self.changed = callback          
        buttons = [Button(name, color = 'secondary', width = 80, close = True) for name in commands]
        for button in buttons:
            button.changed = self.dialog_command_handler      
        buttons[0].color = 'primary' 
        buttons[0].space = True        
        self.icon = icon
        self.value = [[], *content, buttons] if content else buttons        

    async def dialog_command_handler(self, button, _):        
        if user := Unishare.context_user():
            user.active_dialog = None
            # persist=False: sent before self.changed (the actual callback) has run --
            # see User.prepare_result's docstring for why a real persist pass has to
            # wait for the request's genuine end instead of this early notice.
            await user.send(TypeMessage('action', 'close'), persist=False)
            return await call_anysync(self.changed, self, button.name)        

class Screen(Unit):
    def __init__(self, name):
        self._mark_changed = None        
        self.name = name                
        self.type = 'screen'                                  

    def set_reactivity(self, user, override = False):
        super().set_reactivity(user, override)
        for block in flatten(self.blocks):
            block.set_reactivity(user, override)
        for elem in self.toolbar:
            elem.set_reactivity(user, override)
