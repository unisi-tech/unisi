from unisi import *
from blocks.shared import eblock
name = "Notes"
order = 1
persist = True

table = Table("notes", headers = ["Title", "Date"], persist = True, rows = [
    ["Note 1", "2024-01-01"],
    ["Note 2", "2024-01-02"],
    ["Note 3", "2024-01-03"]
])

comment = Edit("Comment", "")
blocks = Block("My Notes",  comment, table), eblock