# Copyright © 2024 UNISI Tech. All rights reserved.
import jsonpickle, inspect, asyncio, warnings

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
    """Call `handler` and await the result if it turns out to be awaitable.

    Deliberately calls first and inspects the *result* rather than
    pre-classifying `handler` with asyncio.iscoroutinefunction(): that
    only recognizes plain `async def` functions/methods, so a perfectly
    ordinary callable *instance* whose `__call__` is a coroutine function
    (a common way to write a stateful handler) would be invoked
    synchronously, handing back an unawaited coroutine object as if it
    were the real result instead of actually running it. Checking the
    result with inspect.isawaitable() after the call covers that case,
    functools.partial-wrapped async functions, and anything else that
    merely *returns* an awaitable, in addition to the plain sync/async
    functions this already handled.
    """
    result = handler(*params)
    if inspect.isawaitable(result):
        result = await result
    return result

def strpath(path):
    "array path to string"
    return '@'.join(path)

def compose_handlers(*handlers):
    async def compose(obj, value):
        objs = set()        
        for handler in handlers:
            result = await call_anysync(handler, obj, value)
            if result == UpdateScreen or result == Redesign:
                return result
            if isinstance(result, list | tuple):
                # NOTE: must not reuse `obj` as the loop variable here --
                # `obj` is compose()'s own parameter (the unit each
                # handler in this chain is invoked with), and a bare
                # `for` loop doesn't get its own scope in Python. Reusing
                # the name would silently overwrite `obj` with the last
                # item of THIS handler's result, so every later handler
                # in the chain would be called with some unrelated
                # changed unit instead of the original one. See
                # tests/core/test_common.py's compose_handlers regression
                # tests for exactly this scenario.
                for item in flatten(result):
                    objs.add(item)
            elif result:
                objs.add(result)
        if objs:
            return list(objs) 
    return compose
    
def equal_dicts(dict1, dict2):
    return dict1.keys() == dict2.keys() and all(dict1[key] == dict2[key] for key in dict1)

class ArgObject:
    # jsonpickle's pickler probes `getattr(obj, '_jsonpickle_exclude', ())`
    # (see jsonpickle.pickler.Pickler._flatten_obj_instance) using
    # getattr()'s 3-argument form, which only falls back to the default
    # when the attribute lookup raises AttributeError. __getattr__ below
    # never raises -- it returns None for *any* missing name -- so without
    # this explicit class attribute the probe finds this class's own
    # __getattr__-synthesized None instead of jsonpickle's own default,
    # and `set(None)` blows up with "TypeError: 'NoneType' object is not
    # iterable" for every single ArgObject (including empty_app) passed to
    # toJson()/jsonpickle.encode. Declaring the real jsonpickle default
    # here as an ordinary class attribute means normal attribute lookup
    # finds it directly and never reaches __getattr__ at all.
    _jsonpickle_exclude = ()

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __getattr__(self, _):
        """return None for unknown props"""
        return None

class ReceivedMessage(ArgObject):
    def __init__(self, kwargs):
        self.__dict__.update(kwargs)
    def __str__(self):
        return f'{self.block}/{self.element}->{self.event}({self.value})'    
    @property
    def screen_type(self):
        return self.block == 'root' and self.element is None
    @property
    def voice_type(self):
        return self.block == 'voice' and self.element is None

def toJson(obj):
    # keys=False is pinned deliberately, not left as an implicit default:
    # jsonpickle 5.0.0 will flip its own default to keys=True, but that
    # mode only makes sense for output meant to be *unpickled* back into
    # Python (it prefixes non-string dict keys with "json://" so the
    # original key type survives a round trip -- e.g. {1: 'x'} becomes
    # {"json://1": "x"}). toJson always calls unpicklable=False -- this
    # output is for a plain JS/JSON consumer that will never round-trip
    # through jsonpickle -- so keys=True would actively corrupt any
    # non-string-keyed dict for that consumer instead of the harmless
    # str(key) coercion keys=False gives it. Suppress just that one
    # warning rather than silence DeprecationWarning wholesale, so any
    # other, unrelated deprecation surfaces normally.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            'ignore', message='keys will default to True', category=DeprecationWarning
        )
        return jsonpickle.encode(obj, unpicklable=False, keys=False)

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
                    #user.log(f'Invisible element update {data.name}, type {data.type}.\n\
                    #    Such element not on the screen!', type = 'warning') #valid
            for inv in invisible:
                self.updates.remove(inv)

    def contains(self, unit):
        if hasattr(self, 'updates'):
            for update in self.updates:
                if unit is update['data']:
                    return True
        return False

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
    """Deletes the unit with the given name from a nested list/tuple of units.

    Returns (found, updated_units). Only the first matching unit
    (depth-first, left to right) is removed; a sub-container left empty by
    the removal is dropped entirely from its parent.

    Every level -- list or tuple, including `units` itself -- is rebuilt
    fresh rather than mutated in place: a plain list could be `.pop()`-ed,
    but a tuple can't be shrunk in place, and `units` is legitimately a
    tuple sometimes (e.g. a screen module's `blocks = block_a, block_b`).
    Rebuilding uniformly means a match works the same way regardless of
    which container types hold it, and the caller (who knows where `units`
    came from) is expected to write `updated_units` back there -- e.g.
    `user.screen.blocks = updated_units` -- since a new tuple obviously
    can't be handed back via in-place mutation.
    `updated_units` mirrors `units`'s own type (tuple in, tuple out; list
    in, list out) at every level; when nothing matches, it's `units`
    unchanged (same contents, rebuilt into a fresh, equal container).
    """
    found = False
    result = []
    for item in units:
        if not found and isinstance(item, list | tuple):
            found, item = delete_unit(item, name)
            if found and not item:  # sub-container is now empty -- drop it
                continue
        elif not found and item.name == name:
            found = True
            continue
        result.append(item)
    return found, (tuple(result) if isinstance(units, tuple) else result)


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
