#Hot connect to running session
from unisi import Proxy, Event

#insert session from Hello user output' #
session = '::1-0'

sname = 'Panda params'

proxy = Proxy('localhost:8000', screen = sname, timeout = 7)
if proxy.event == Event.screen:
    if proxy.screen['name'] != sname:
        print('invalide screen')
        
    if proxy.event & Event.update:
        proxy.set_value('Edit string','abc')
        print('ok')
    else:
        print('error')

proxy.close()
