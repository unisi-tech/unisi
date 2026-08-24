from unisi import *

name = "Home"
order = 0

result_edit = Edit("Result", "")


def on_save(elem, value):
    # A real side-effect: changes a DIFFERENT element than the one the
    # message targeted, so the response the client receives carries an
    # `updates` entry built from changed_units -- exactly the shape the
    # Recorder round-trip tests need to exercise.
    result_edit.value = f"saved:{value}"
    return None


save_button = Button("Save", on_save)
plain_edit = Edit("Plain", "default")

root = Block("Root", save_button, plain_edit, result_edit)
blocks = [root]
