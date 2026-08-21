from unisi import *

# a reusable unit imported (by reference) from more than one screen -- the
# same object every time, for the same *user*, since blocks are cached per
# user via user.modules and re-installed into sys.modules around each
# compile_screen() call (see ModulesMixin._install_modules/_capture_modules).
shared_label = Text("Shared widget content")

widget_block = Block("Widget", shared_label)
