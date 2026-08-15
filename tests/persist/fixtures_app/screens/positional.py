from unisi import *

name = "Positional"
order = 0

# classic positional persist -- saved/restored automatically whenever changed
flagged = Edit("Flagged", "default", persist=True)

# no persist flag at all: for persist_units/restore_units targets and as a
# baseline "nothing should be saved for this" control
plain = Edit("Plain", "unflagged")
inner = Edit("Inner", "inner-default")
plain_block = Block("Plain block", plain, inner)   # container, no persist flag either

blocks = [Block("Root", flagged, plain_block)]
