from unisi import *

name = "Home"
order = 0

# --- explicit handler, for process()/process_element()/eval_handler dispatch ---
save_clicks = []
def on_save(elem, value):
    save_clicks.append(value)
    return "handled"

save_button = Button("Save", on_save)

# --- no explicit handler: process_element's default "just assign .value" path ---
plain_edit = Edit("Plain", "default")

# --- carries an id -- for calc_dbsharing()/sync_dbupdates() ---
shared_edit = Edit("Shared", "shared-default", id=4242)

# --- a non-'changed' event handled via a matching attribute on the unit itself ---
completable = Edit("Completable", "1")
async def on_complete(elem, value):
    return "completion-result"
completable.complete = on_complete

attributed = Edit("Attributed", "1")
attributed.custom_attr = "old"

root = Block("Root", save_button, plain_edit, shared_edit, completable, attributed)
blocks = [root]

help_button = Button("Help")
toolbar = [help_button]
