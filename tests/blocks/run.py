from aiohttp import web
import unisi

async def handle_get(request):
    print(request.query_string)

http_handlers = [web.get('/get', handle_get)]

unisi.start('Test app', http_handlers = http_handlers)
