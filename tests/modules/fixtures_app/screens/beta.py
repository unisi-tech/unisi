from unisi import *

name = "Beta"
order = 1

prepared = []
def prepare():
    prepared.append(1)

field = Edit("Field", "beta-value")
blocks = [Block("Root", field)]
