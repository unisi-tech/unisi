import sys
import os
from aiohttp import web

#optional:  add 2 path if unisi is installed near (for deep testing or developing)
wd = os.getcwd()
sys.path.insert(0,wd[:wd.find('/unisi')] + '/unisi')
print(wd[:wd.find('/unisi')] + '/unisi')

import unisi

async def handle_get(request):
    print(request.query_string)

http_handlers = [web.get('/get', handle_get)]

class Hello_user(unisi.User):
    def __init__(self):
        super().__init__()        
        print('New Hello user connected and created!')

unisi.start('Test app', user_type = Hello_user, http_handlers = http_handlers)
