import os, sys

#optional:  add 2 path if unisi is installed near (for deep testing or developing)
wd = os.getcwd()
print(wd[:wd.find('/unisi')] + '/unisi')
sys.path.insert(0,wd[:wd.find('/unisi')] + '/unisi')

from unisi import *

for i in range(1000):
    proxy = Proxy('localhost:8000')
    ok = proxy.set_screen('Zoo')
    proxy.close()
