from unisi import *

name = "Other"
order = 1

prepared = []
def prepare():
    prepared.append(1)

field = Edit("Field", "other-value")
blocks = [Block("Root", field)]
