# Copyright © 2024 UNISI Tech. All rights reserved.
import os, sys, types, tempfile, atexit, shutil, platform, requests, logging
from .common import set_defaults
from .containers import Screen

blocks_dir = 'blocks'        
screens_dir =  'screens'        
testdir = 'autotest'

divpath = '\\' if platform.system() == 'Windows' else '/'
libpath = os.path.dirname(os.path.realpath(__file__))
webpath = f'{libpath}{divpath}web' 
app_dir = os.getcwd()

try:
    import config
except:
    if os.path.exists('config.py'):
        print('Invalid script is started! It has to be in a working directory.')
        exit()
    if 'pytest' in sys.modules:
        # Running under pytest: importing unisi must not write config.py to
        # whatever directory pytest happens to be invoked from (usually a
        # project's repo root, not any test's own directory) -- nor, via the
        # logfile/upload_dir settings below, a 'log' file or a 'web'
        # directory alongside it. Build the same default config in memory
        # instead of on disk, only redirecting the two settings that write
        # to the filesystem: logfile (None disables file logging entirely,
        # see start_logging() below) and upload_dir (a throwaway per-process
        # tmp directory instead of the relative 'web'). Everything else
        # matches the on-disk default in the 'else' branch exactly.
        config = types.ModuleType('config')
        config.port = 8000
        config.upload_dir = tempfile.mkdtemp(prefix='unisi_upload_')
        config.hot_reload = True
        config.logfile = None
        config.autotest = '*'
        config.appname = 'Unisi app'
        sys.modules['config'] = config
        atexit.register(shutil.rmtree, config.upload_dir, ignore_errors=True)
    else:
        f = open('config.py', 'w')  
        f.write("""port = 8000 
upload_dir = 'web'
hot_reload  = True
logfile  = 'log'
autotest = '*'
appname = 'Unisi app'
""")
        f.close()
        import config
        print("Config with default parameters is created!")

#setting config variables
set_defaults(config,  dict(
    autotest= False,
    appname = 'Unisi app',
    upload_dir = 'web',
    logfile= None,
    hot_reload = False,    
    mirror = False,
    share = False,
    profile = 0, 
    llm = None,
    froze_time= None,
    monitor_tick = 0.005,
    pool = None,
    db_path = None,
    lang = 'en-US',
    public_dirs = [],
    debug = False,
    session = None,
    image = 'icons/favicon-32x32.png'
))

Screen.defaults = dict(
    icon = None,
    prepare = None,            
    blocks = [],
    header = config.appname,                        
    toolbar = [], 
    order = 0,
    persist = False,
    reload = config.hot_reload, 
    lang = config.lang,
    voice = not config.mirror,
    image = config.image
)

if config.froze_time == 0:
    print('froze_time in config.py can not be 0!')
    config.froze_time = None

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

def iter_layout_units(value):
    """Recursively yield all Unit objects in a layout tree."""
    from .units import Unit, ChangedProxy
    if isinstance(value, ChangedProxy):
        value = value._obj
    if isinstance(value, list | tuple):
        for item in value:
            yield from iter_layout_units(item)
    elif isinstance(value, Unit):
        yield value
        if getattr(value, 'type', None) == 'block' and hasattr(value, 'value'):
            yield from iter_layout_units(value.value)

def fill_parents(value, parent, parents):
    """Recursively populate parents dict {unit: containing_unit}."""
    from .units import Unit, ChangedProxy
    if isinstance(value, ChangedProxy):
        value = value._obj
    if isinstance(value, list | tuple):
        for item in value:
            fill_parents(item, parent, parents)
    elif isinstance(value, Unit):
        parents[value] = parent
        if getattr(value, 'type', None) == 'block' and hasattr(value, 'value'):
            fill_parents(value.value, value, parents)

def py_files(directory):
    """Yield .py filenames in directory, excluding __init__.py."""
    if os.path.exists(directory):
        for file in os.listdir(directory):
            if file.endswith('.py') and file != '__init__.py':
                yield file

def start_logging(): 
    format = "%(asctime)s - %(levelname)s - %(message)s"
    logfile = config.logfile
    handlers = [logging.FileHandler(logfile), logging.StreamHandler()] if logfile else []
    logging.basicConfig(level = logging.WARNING, format = format, handlers = handlers)    

start_logging()