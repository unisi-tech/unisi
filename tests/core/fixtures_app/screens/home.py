from unisi import *

name = "Home"
order = 0

clicks = []
def on_save(elem, value):
    clicks.append(value)
    return "saved"

save_button = Button("Save", on_save)
plain_edit = Edit("Plain", "default")

root = Block("Root", save_button, plain_edit)
blocks = [root]

help_button = Button("Help")
toolbar = [help_button]
