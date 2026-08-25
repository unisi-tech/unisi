from aiohttp import web
import unisi

async def handle_get(request):
    print(request.query_string)

http_handlers = [web.get('/get', handle_get)]

class Hello_user(unisi.User):
    def __init__(self, session: str, share = None, screen: str | None = None):
        super().__init__(session, share, screen)
        print('New Hello user connected and created, session: {}'.format(session))

unisi.start(user_type = Hello_user, http_handlers = http_handlers)
