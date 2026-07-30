from unisi import *
name = "Animals"
order = 0

table = Table("animals", headers = ["name", "age", 'positive'], rows = [
    ["Dog", 5, True],
    ["Cat", 3, False],
    ["Bird", 2, True]
], persist = True)

def row_key():
    index = table.value
    if index is not None:
        return table.rows[index][0]

link = Edit('wiki link', '', persist = row_key)
blocks = Block('My animals', link, table)