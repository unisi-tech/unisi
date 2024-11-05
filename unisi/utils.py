# Copyright © 2024 UNISI Tech. All rights reserved.
import os, platform, requests, logging
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
    db_dir = None,
    lang = 'en-US',
    public_dirs = [],
    debug = False
))

Screen.defaults = dict(
    icon = None,
    prepare = None,            
    blocks = [],
    header = config.appname,                        
    toolbar = [], 
    order = 0,
    reload = config.hot_reload, 
    lang = config.lang
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

def start_logging(): 
    format = "%(asctime)s - %(levelname)s - %(message)s"
    logfile = config.logfile
    handlers = [logging.FileHandler(logfile), logging.StreamHandler()] if logfile else []
    logging.basicConfig(level = logging.WARNING, format = format, handlers = handlers)    

start_logging()



