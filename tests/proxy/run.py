import os, sys

#optional
import dev

from unisi import *

for i in range(1000):
    proxy = Proxy('localhost:8000')
    ok = proxy.set_screen('Zoo')
    proxy.close()
