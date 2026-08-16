from unisi import *

# automatically persisted -- persist=True on the block cascades to its whole subtree
auto_edit = Edit("Auto edit", "auto-default")
auto_block = Block("Auto shared block", auto_edit, persist=True)

# NOT automatically persisted -- target for persist_units/restore_units on a shared unit
manual_edit = Edit("Manual edit", "manual-default")
manual_block = Block("Manual shared block", manual_edit)
