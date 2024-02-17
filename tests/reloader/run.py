import os, sys

#optional:  add 2 path if unisi is installed near (for deep testing or developing)
wd = os.getcwd()
print(wd[:wd.find('/unisi')] + '/unisi')
sys.path.insert(0,wd[:wd.find('/unisi')] + '/unisi')

import unisi
unisi.start('Test app')