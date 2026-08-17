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

# Block nested inside another Block (two levels below the screen), no persist
# flag anywhere in the chain -- for persist_units/restore_units tests that
# exercise recursion deeper than one level, and as a shared target for
# context_key round-trip tests (see test_persist_units.py).
leaf_a = Edit("Leaf A", "leaf-a-default")
leaf_b = Edit("Leaf B", "leaf-b-default")
inner_nested_block = Block("Inner nested block", leaf_a, leaf_b)
outer_nested_block = Block("Outer nested block", inner_nested_block)
blocks.append(outer_nested_block)
