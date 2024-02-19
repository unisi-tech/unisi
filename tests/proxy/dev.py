#optional:  add 2 path if unisi is installed near (for deep testing or developing)
import sys, os
wd = os.getcwd()
sys.path.insert(0,wd[:wd.find('/unisi')] + '/unisi')
print(wd[:wd.find('/unisi')] + '/unisi')