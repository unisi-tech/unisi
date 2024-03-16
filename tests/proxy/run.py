#Interact with tests/blocks

from unisi import Proxy

proxy = Proxy('localhost:8000')

proxy.elements()

commands = proxy.commands

ok = proxy.set_screen('Zoo')

proxy.close()
