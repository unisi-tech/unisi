#Hot connect to running session
from unisi import Proxy, Event

session = 'insert session from Hello user output' #'::1-0'

proxy = Proxy('localhost:8000', session = session, timeout = 7)
if proxy.event == Event.screen:
    if proxy.screen['name'] != 'Main':
        proxy.set_screen('Main')
    if proxy.event & Event.update:
        proxy.set_value('Edit string','abc')
        print('ok')
    else:
        print('error')

proxy.close()
