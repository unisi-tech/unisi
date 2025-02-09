# Copyright © 2024 UNISI Tech. All rights reserved.
import jsonpickle, inspect, asyncio

UpdateScreen = True
Redesign = 2

def flatten(*arr):
    for a in arr:
        if isinstance(a, list | tuple):
            yield from flatten(*a)
        else:
            yield a

def index_of(lst, target):  
  try:
    return lst.index(target)
  except ValueError:
    return -1
  
async def call_anysync(handler, *params):
    return (await handler(*params)) if asyncio.iscoroutinefunction(handler) else handler(*params)

def compose_handlers(*handlers):
    async def compose(obj, value):
        objs = set()        
        for handler in handlers:
            result = await call_anysync(handler, obj, value)
            if result == UpdateScreen or result == Redesign:
                return result
            if isinstance(result, list | tuple):
                for obj in flatten(result):
                    objs.add(obj)
            elif result:
                objs.add(result)
        if objs:
            return list(objs) 
    return compose
    
def equal_dicts(dict1, dict2):
    return dict1.keys() == dict2.keys() and all(dict1[key] == dict2[key] for key in dict1)

class ArgObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __getattr__(self, _):
        """return None for unknown props"""
        return None
    @property
    def screen_type(self):
        return self.block == 'root' and self.element is None
    @property
    def voice_type(self):
        return self.block == 'voice' and self.element is None

class ReceivedMessage(ArgObject):
    def __init__(self, kwargs):
        self.__dict__.update(kwargs)
    def __str__(self):
        return f'{self.block}/{self.element}->{self.event}({self.value})'    

def toJson(obj):
    return jsonpickle.encode(obj,unpicklable = False)

def set_defaults(self, param_defaults : dict):
    for param, value in param_defaults.items():
        if not hasattr(self, param):
            setattr(self, param, value)

def pretty4(name):
    if name.startswith('_'):
        name = name[1:]
    pretty_name = name.replace('_',' ')
    return pretty_name[0].upper() + pretty_name[1:]

def is_callable(obj):
    return inspect.isfunction(obj) or inspect.ismethod(obj) or inspect.iscoroutine(obj) or callable(obj)        

def context_object(target_type):
  """
  Finds the first argument of a specific type in the current function call stack.
  """  
  frame = inspect.currentframe()
  while frame:    
    args, _, _, values = inspect.getargvalues(frame)    
    if args and isinstance(values[args[0]], target_type):
      return values[args[0]]
    # Move to the previous frame in the call stack
    frame = frame.f_back
  return None

def get_default_args(func):
    """  
    class F:
        def example_function(a, b, c=10, d='hello'):
            pass
    f = F()
    default_args = get_default_args(f.example_function)
    print(default_args)  
    """
    # Get the signature of the function
    sig = inspect.signature(func)
    # Dictionary to store arguments with their default values
    defaults = {}
    for name, param in sig.parameters.items():
        if param.default != inspect.Parameter.empty:
            defaults[name] = param.default
    return defaults

Unishare = ArgObject(context_user = lambda: None, sessions = {})

class Message:
    def __init__(self, *units, user = None, type = 'update'):        
        self.type = type        
        self.set_updates(units)
        if user:
            self.fill_paths4(user)

    def set_updates(self, units):
        self.updates = [{'data': unit} for unit in units]

    def fill_paths4(self, user):
        if hasattr(self, 'updates'):
            invisible = []
            for update in self.updates:
                data = update["data"]
                path = user.find_path(data)
                if path:
                    update['path'] = path
                else:
                    invisible.append(update)                    
                    user.log(f'Invisible element update {data.name}, type {data.type}.\n\
                    Such element not on the screen!', type = 'warning')
            for inv in invisible:
                self.updates.remove(inv)

    def contains(self, unit):
        if hasattr(self, 'updates'):
            for update in self.updates:
                if unit is update['data']:
                    return True

def TypeMessage(type, value, *data, user = None):
    message = Message(*data, user=user, type = type)    
    message.value = value
    return message    

def Warning(text, *data):
    return TypeMessage('warning', str(text), *data)

def Error(text, *data):
    return TypeMessage('error', str(text), *data)
    
def Info(text, *data):
    return TypeMessage('info', str(text), *data)

def Answer(type, message, result):
    ms = TypeMessage(type, result)
    ms.message = message
    return ms

close_message = TypeMessage('action', 'close')

def delete_unit(units, name):
    """Deletes a unit with the given name from a nested list of units.
        Returns True if the unit was found and deleted, False otherwise.
    """
    for i in range(len(units)):
        if isinstance(units[i], list | tuple):
            if delete_unit(units[i], name):
                if not units[i]: # if the sublist became empty after deletion
                    units.pop(i) # remove sublist also
                return True
        elif units[i].name == name:
            units.pop(i) 
            return True
    return False

empty_app = ArgObject(
    blocks = [],
    header = "No screens",
    icon = None,
    menu = [["You need to put at least 1 file in the 'screens' folder.",'exclamation']],
    name = "",
    order = 0,
    toolbar = [],
    type = "screen"
)




