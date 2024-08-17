from unisi import *
from data import *
name = 'Linked tables'
order = 1

utable = Table("Users", id = 'User', limit = 150, ids = True,
    rows= users, headers=['name', 'age', 'height'])
otable = Table("Orders", id = 'Orders', limit = 150, ids = True, 
    rows= orders, headers=['name', 'sum'], link = (utable, {'type' : 'STRING', 'weight' : 'DOUBLE'}))
blocks = [Block('TBlock', [], [utable, otable])]