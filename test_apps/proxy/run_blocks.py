#Hot connect to running session and read and change values in screen
from unisi import Proxy, Event

# !Importent for test!
# 1. run blocks test app and switch to Panda params screen
# 2. insert session from Hello user output to 'session variable below' #

session = 'C0UqQkSisd-0' # <-- insert session from Hello user output here

sname = 'Panda params'

proxy = Proxy('localhost:8000', session = session, screen = sname, timeout = 7)
if proxy.event == Event.screen:
    if proxy.screen['name'] != sname:
        print('invalide screen')
        
    if proxy.event & Event.update:
        proxy.set_value('Edit string','123')
        print('ok')
    else:
        print('error')

proxy.close()
