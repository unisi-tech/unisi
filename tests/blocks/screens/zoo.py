from data import table
from unisi import *
name = 'Zoo'
order = 1

zoo_table = Table('Zoo Table', panda = table)

ext_rows = [row * 2 for row in zoo_table.rows]

sec_table = Table('Sec table', rows = ext_rows, headers = zoo_table.headers._obj)

blocks = [Block('Csv table', [], zoo_table,sec_table)]

def prepare():
    sec_table.name += ' is prepared!'

