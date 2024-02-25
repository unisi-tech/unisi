#Interact with tests/blocks

#optional
import dev

from unisi import Proxy

proxy = Proxy('localhost:8000')

proxy.elements()

commands = proxy.commands



ok = proxy.set_screen('Zoo')

proxy.close()
