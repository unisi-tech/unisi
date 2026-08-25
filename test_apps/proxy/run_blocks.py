#Hot connect to running session and read and change values in screen
from unisi import Proxy, Event

# !Importent for test!
# 1. run blocks test app and switch to Panda params screen
# 2. insert session from Hello user output' #

session = 'E0IKyrkVUB-0'

sname = 'Panda params'

proxy = Proxy('localhost:8000', session = session, screen = sname, timeout = 7)
if proxy.event == Event.screen:
    if proxy.screen['name'] != sname:
        print('invalide screen')
        
    if proxy.event & Event.update:
        proxy.set_value('Edit string','abc')
        print('ok')
    else:
        print('error')

proxy.close()
