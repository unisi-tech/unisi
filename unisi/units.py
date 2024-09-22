from .common import *
from .llmrag import get_property

atomics = (int, float, complex, bool, str, bytes, type(None))

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

    def __setitem__(self, key, value):
        self._obj[key] = value
        self._unit._mark_changed ()
       
    def __getitem__(self, key):
        return self._obj[key]    

    def __delitem__(self, key):
        del self._obj[key]
        self._unit._mark_changed ()

    def __iter__(self):
        return iter(self._obj)

    def __len__(self):
        return len(self._obj)
    
    def __getstate__(self):     
        return self._obj
           
class Unit:    
    def __init__(self, name, *args, **kwargs):                
        self._mark_changed =  None
        self.name = name
        la = len(args)
        if la:
            self.value = args[0]
        if la > 1:
            self.changed = args[1]                    
        self.add(kwargs)

    def set_reactivity(self, user, override = False):        
        if not hasattr(self, 'id') and (override or not self._mark_changed): 
            def changed_call(property = None, value = None):
                user.register_changed_unit(self, property, value)
            super().__setattr__('_mark_changed', changed_call)

    def add(self, kwargs):              
        for key, value in kwargs.items():
            setattr(self, key, value)

    def mutate(self, obj):
        self.__dict__ = obj.__dict__ 
        if self._mark_changed:
            self._mark_changed()

    def __setattr__(self, name, value):        
        if self._mark_changed and name != "_mark_changed":
            if name != "__dict__" and not isinstance(value, atomics) and not callable(value):
              value = ChangedProxy(value, self)                                    
            self._mark_changed(name, value)
        super().__setattr__(name, value)

    def mutate(self, obj):
        for key, value in obj.__dict__.items():
            if not key.startswith('__'):
                setattr(self, key, value)
        if self._mark_changed:
            self._mark_changed()
    
    def accept(self, value):
        if hasattr(self, 'changed'):
            self.changed(self, value)
        else:
            self.value = value

    @property
    def compact_view(self) -> str:
        """reduce for external (llm) using if required"""
        return f'{self.name} : {self.value}'
    
    async def emit(self, *_ ):        
        """calcute value by system llm, can be used as a handler"""
        if Unishare.llm_model and (exactly := getattr(self, 'llm', None)) is not None:        
            elems = [e.compact_view for e in self.__llm_dependencies__ if e.value != '' and e.value is not None]
            #exactly is requirment that all elements have to have valid value
            if not exactly or len(elems) == len(self.__llm_dependencies__):
                context = ','.join(elems)    
                self.value = await get_property(self.name, context, self.type, options = getattr(self, 'options', None))
                return self
            
    def add_changed_handler(self, handler):
        changed_handler = getattr(self, 'changed', None)
        if not changed_handler:
            def changed_handler(obj, value):
                obj.value = value
        self.changed = compose_handlers(changed_handler, handler) 

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

class Node:
    def __init__(self, name = '',id = '', color = '', size = 0):
        if name:
            self.name = name
        if color:
            self.color = color
        if size:
            self.size = size
        if id:
            self.id = id        

class Edge:
    def __init__(self, source, target, name = '', id = '', color = '', size = 0):
        self.source = source
        self.target = target
        if name:
            self.name = name
        if color:
            self.color = color
        if size:
            self.size = size
        if id:
            self.id = id        

graph_default_value = {'nodes' : [], 'edges' : []}

class Graph(Unit):
    '''has to contain nodes, edges, see Readme'''
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        self.type='graph'
        set_defaults(self,{'value': graph_default_value, 'nodes': [], 'edges': []})
        
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
        self.type = 'tree' 
        set_defaults(self,{'options': [], 'value': None})
        
class TextArea(Unit):
    def __init__(self,name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        self.type = 'text' 
                     
        