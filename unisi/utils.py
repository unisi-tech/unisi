import os, platform, requests, inspect, logging

blocks_dir = 'blocks'        
screens_dir =  'screens'        
UpdateScreen = True
Redesign = 2
public_dirs = 'public_dirs'
testdir = 'autotest'

divpath = '\\' if platform.system() == 'Windows' else '/'
libpath = os.path.dirname(os.path.realpath(__file__))
webpath = f'{libpath}{divpath}web' 
app_dir = os.getcwd()

try:
    import config
except:
    f = open('config.py', 'w')  
    f.write("""port = 8000 
upload_dir = 'web'
hot_reload   = True
logfile  = 'log'
autotest = '*'
appname = 'Unisi app'
""")
    f.close()
    import config
    print("Config with default parameters is created!")

#setting config variables
defaults = {
    testdir: False,
    'appname' : 'Unisi app',
    'upload_dir' : 'web',
    'logfile': None,
    'hot_reload' : False,    
    'mirror' : False,
    'share' : False,
    'profile' : 0, 
    'rag' : None,
    'froze_time': None,
    'monitor_tick' : 0.005,
    'pool' : None
}
for param, value in defaults.items():
    if not hasattr(config, param):
       setattr(config, param, value)
#froze_time can not be 0
if config.froze_time == 0:
    config.froze_time = None

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

def is_screen_switch(message):
    return message and message.block == 'root' and message.element is None

def filename2url(fn):   
    if fn[0] == '/' or fn[1] == ':': #if full path
        fn = fn[len(app_dir):]   
    if fn[0] == divpath:
        fn = fn[1:]
    return fn 

def url2filepath(url):
    return url[url.find('/') + 1:].replace('%20',' ')   

def url2filename(url):
    return url[url.rfind('/') + 1:].replace('%20',' ')   

def upload_path(fpath):
    return f'{config.upload_dir}{divpath}{fpath}'
    
def cache_url(url):
    """cache url file in upload_dir and returns the local file name"""
    fname = url2filename(url)   
    fname = upload_path(fname)
    response = requests.get(url)
    if response.status_code != 200:
        return None
    file = open(fname, "wb")
    file.write(response.content)
    file.close() 
    return fname

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

def start_logging(): 
    format = "%(asctime)s - %(levelname)s - %(message)s"
    logfile = config.logfile
    handlers = [logging.FileHandler(logfile), logging.StreamHandler()] if logfile else []
    logging.basicConfig(level = logging.WARNING, format = format, handlers = handlers)    

start_logging()



