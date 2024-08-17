import jsonpickle, inspect

UpdateScreen = True
Redesign = 2

def flatten(*arr):
    for a in arr:
        if isinstance(a, list | tuple):
            yield from flatten(*a)
        else:
            yield a

def compose_returns(*arr):
    objs = set()
    update_screen = False
    for obj in flatten(*arr):
        if obj is Redesign:
            return obj
        elif obj is True:
            update_screen = True
        elif obj is not None:
            objs.add(obj)            
    if update_screen:
        return True
    if objs:
        return list(objs)
    
def equal_dicts(dict1, dict2):
    return dict1.keys() == dict2.keys() and all(dict1[key] == dict2[key] for key in dict1)

class ArgObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __getattr__(self, _):
        """return None for unknown props"""
        return None

class ReceivedMessage(ArgObject):
    def __init__(self, kwargs):
        super().__init__(**kwargs)
    def __str__(self):
        return f'{self.block}/{self.element}->{self.event}({self.value})'

def toJson(obj):
    return jsonpickle.encode(obj,unpicklable = False)

def set_defaults(self, param_defaults : dict):
    for param, value in param_defaults.items():
        if not hasattr(self, param):
            setattr(self, param, value)

def pretty4(name):
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

references = ArgObject(context_user = None)

class Message:
    def __init__(self, *gui_objects, user = None, type = 'update'):        
        self.type = type
        if gui_objects:
            self.updates = [{'data': gui} for gui in gui_objects]
        if user:
            self.fill_paths4(user)

    def fill_paths4(self, user):
        if hasattr(self, 'updates'):
            invalid = []
            for update in self.updates:
                data = update["data"]
                path = user.find_path(data)
                if path:
                    update['path'] = path
                else:
                    invalid.append(update)                    
                    user.log(f'Invalid element update {data.name}, type {data.type}.\n\
                    Such element not on the screen!')
            for inv in invalid:
                self.updates.remove(inv)

    def contains(self, guiobj):
        if hasattr(self, 'updates'):
            for update in self.updates:
                if guiobj is update['data']:
                    return True

def TypeMessage(type, value, *data, user = None):
    message = Message(*data, user=user, type = type)    
    message.value = value    
    return message    

def Warning(text, *data):
    return TypeMessage('warning', text, *data)

def Error(text, *data):
    return TypeMessage('error', text, *data)
    
def Info(text, *data):
    return TypeMessage('info', text, *data)

def Answer(type, message, result):
    ms = TypeMessage(type, result)
    ms.message = message
    return ms





