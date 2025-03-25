# Copyright © 2024 UNISI Tech. All rights reserved.
from .common import *
from .llmrag import get_property

class ChangedProxy:
    MODIFYING_METHODS = {
        'append', 'extend', 'insert', 'remove', 'pop', 'clear', 'sort', 'reverse',
        'update', 'popitem', 'setdefault', '__setitem__', '__delitem__'
    }
    def __init__(self, obj, unit):
        self._obj = obj
        self._unit = unit
    
    def __getattribute__(self, name):        
        if name == '_obj' or name == '_unit' or name == '__getstate__':
            return super().__getattribute__(name)
        obj = super().__getattribute__('_obj')
        value = getattr(obj, name)  
        if isinstance(value, ChangedProxy):
            value = value._obj
        if name in ChangedProxy.MODIFYING_METHODS:
            super().__getattribute__('_unit')._mark_changed()
        elif not callable(value) and not isinstance(value, atomics):
            return ChangedProxy(value, self)
        return value
    
    def __setattr__(self, name, value):                
        if name.startswith('_'):
            super().__setattr__(name, value)
        else:
            self._obj.__setattr__(name, value) 
            self._unit._mark_changed()

    def __setitem__(self, key, value):
        self._obj[key] = value
        self._unit._mark_changed ()
       
    def __getitem__(self, key):
        value = self._obj[key]    
        if not callable(value) and not isinstance(value, atomics):
            value = ChangedProxy(value, self._unit)
        return value
    
    def __eq__(self, other):
        return self._obj.__eq__(other._obj) if isinstance(other, ChangedProxy) else self._obj.__eq__(other)

    def __delitem__(self, key):
        del self._obj[key]
        self._unit._mark_changed ()

    def __iter__(self):
        return iter(self._obj)

    def __len__(self):
        try:
            return len(self._obj)
        except TypeError:        
            return 0  
        
    def __hash__(self):
        return hash(self._obj)
        
    def __iadd__(self, other):  
        if isinstance(self._obj, list):
            self.extend(other)  
            return self  # Important: __iadd__ must return self
        
        raise TypeError(f"Unsupported operand type for += with '{type(self._obj).__name__}'")
            
    def __getstate__(self):     
        return self._obj
    
atomics = (int, float, complex, bool, str, bytes, ChangedProxy, type(None))
           
class Unit:    
    action_list = set(['complete', 'update', 'changed','delete','append', 'modify'])
    def __init__(self, name, *args, **kwargs):                
        self._mark_changed =  None
        self.name = name
        la = len(args)
        if la:
            self.value = args[0]
        if la > 1:
            self.changed = args[1]                    
        self.add(kwargs)

    def specific_changed_register(self, property, value) -> bool:
        """ addtional actions when changed, return False if not changed"""
        return not property or not property.startswith('_')
        
    def set_reactivity(self, user, override = False):        
        changed_call = None
        
        if not hasattr(self, 'id') and (override or not self._mark_changed): 
            self.__dict__.update({property : ChangedProxy(value, self)  for property, value in self.__dict__.items() 
                if property[0] != '_' and not isinstance(value, atomics) and not callable(value)})                    
                    
            def changed_call(property = None, value = None):
                if self.specific_changed_register(property, value):
                    user.register_changed_unit(self, property, value)            
        super().__setattr__('_mark_changed', changed_call)                    

    def add(self, kwargs):              
        for key, value in kwargs.items():
            setattr(self, key, value)   

    def __setattr__(self, name, value):      
        #it is correct condition order 
        if name[0] != "_" and self._mark_changed:            
            self._mark_changed(name, value)
        super().__setattr__(name, value)

    def mutate(self, obj):
        if self is not obj:
            self.__dict__.clear()
            for key, value in obj.__dict__.items():
                setattr(self, key, value)
            if self._mark_changed:
                self._mark_changed()
    
    def accept(self, value):
        if hasattr(self, 'changed'):
            self.changed(self, value)
        else:
            self.value = value

    def delattr(self, attr):
        if hasattr(self, attr): 
            delattr(self, attr)

    def __eq__(self, other):
        return super().__eq__(other._obj) if isinstance(other, ChangedProxy) else super().__eq__(other)
    
    def __hash__(self):
        return super().__hash__()
    
    @property
    def compact_view(self) -> str:
        """reduce for external (llm) using if required"""
        return f'{self.name} : {self.value}'
    
    @property
    def type_value(self):
        """return python system type if value not 'date' type"""
        return type(self.value) if self.type != 'date' else 'date'
    
    async def emit(self, *_ ):        
        """calcute value by system llm, can be used as a handler"""
        if Unishare.llm_model and (exactly := getattr(self, 'llm', None)) is not None:        
            elems = [e.compact_view for e in self._llm_dependencies if e.value != '' and e.value is not None]
            #exactly is requirment that all elements have to have valid value
            if not exactly or len(elems) == len(self._llm_dependencies):
                context = ','.join(elems)    
                self.value = await get_property(self.name, context, self.type_value, options = getattr(self, 'options', None))
                return self
            
    def add_changed_handler(self, handler):
        changed_handler = getattr(self, 'changed', None)
        if not changed_handler:
            def changed_handler(obj, value):
                obj.value = value
        self.changed = compose_handlers(changed_handler, handler) 
    
    def __getstate__(self):         
        return {n: (True if n in Unit.action_list else v) for n, v in self.__dict__.items() if n[0] != '_'}

    def __str__(self):
        return f'{type(self).__name__}({self.name})'
    
    def __repr__(self):
        return f'{type(self).__name__}({self.name})'

Line = Unit("__Line__", type = 'line')

def smart_complete(lst, min_input_length = 0, max_output_length = 20):
    di = {it: it.lower() for it in lst}
    def complete(_, ustr):
        if len(ustr) < min_input_length:
            return []
        ustr = ustr.lower()
        arr = [(itlow.find(ustr), it, itlow) for it, itlow in di.items() if itlow.find(ustr) != -1]
        arr.sort(key = lambda e: (e[0], e[2]))
        if len(arr) > max_output_length:
            arr = arr[: max_output_length]
        return [e[1] for e in arr]
    return complete

class Edit(Unit):
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)        
        self.x = 0
        has_value = hasattr(self,'value')
        if 'type' not in kwargs:            
            if has_value:
                type_value = type(self.value)
                if type_value == int or type_value == float:
                    self.type = 'number'
                    return
            self.type = 'string'
        if not has_value:
            self.value = '' if self.type != 'number' else 0

class Text(Unit):
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        self.value = self.name
        self.type = 'string'
        self.edit = False       

class Range(Unit):
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)    
        if not hasattr(self, 'value'):
            self.value = 1.0    
        self.type = 'range'                
        if 'options' not in kwargs:
            self.options = [self.value - 10, self.value + 10, 1]

class ContentScaler(Range):
    def __init__(self, *args, **kwargs):
        name = args[0] if args else 'Scale content'        
        super().__init__(name, *args, **kwargs)                
        if 'options' not in kwargs:
            self.options = [0.25, 3.0, 0.25]
        self.changed = self.scaler
        
    def scaler(self, _, val):
        prev = self.value
        elements = self.elements() 
        self.value = val
        if elements:
            prev /= val
            for element in elements:
                element.width /= prev
                element.height /= prev            
            return elements

class Button(Unit):
    def __init__(self, name, handler = None, **kwargs):
        self._mark_changed =  None
        self.name = name
        self.value = None
        self.add(kwargs)
        if not hasattr(self, 'type'):
            self.type = 'command'
        if handler:
            self.changed = handler
            
def CameraButton(name, handler = None, **kwargs):    
    kwargs['type'] = 'camera'
    return Button(name, handler, **kwargs)
        
def UploadButton(name, handler = None,**kwargs):    
    kwargs['type'] = 'uploader'
    if 'width' not in kwargs:
        kwargs['width'] = 250.0                  
    return Button(name, handler, **kwargs)

class Image(Unit):
    '''name is file name or url, label is optional text to draw on the image'''
    def __init__(self, name, value = False, handler = None, label = '', width = 300, **kwargs):
        super().__init__(name, [], **kwargs)
        self.value = value
        if handler:
            self.changed = handler
        self.label = label
        self.type='image'        
        self.width = width        
        if not hasattr(self,'url'):
            self.url = self.name
        #mask full win path from Chrome detector
        if self.url[1] == ':': 
            self.url = f'/{self.url}'

class Video(Unit):
    '''has to contain src parameter'''
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        self.type = 'video'        
        set_defaults(self, {'url': self.name, 'ratio' : None})                    
        
class Switch(Unit):
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)        
        set_defaults(self,{'value': False, 'type': 'switch'})

class Select(Unit):
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        set_defaults(self,{'options': [], 'value': None})
        if not hasattr(self, 'type'):
            self.type = 'select' if len(self.options) > 3 else 'radio'        

class Tree(Unit):
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)                 
        set_defaults(self,dict(options = [], value = None, type = 'tree'))
        
class TextArea(Unit):
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        self.x = 0
        self.type = 'text' 

class HTML(Unit):
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)        
        self.type = 'html' 
                     
        