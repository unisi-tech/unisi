from unisi import *
name = 'Main'

table = Table('Persons', headers = ['Name', 'Date of birth','Occupation'], 
                llm = {'Date of birth': 'Name', 'Occupation': True},
    rows = [['Michael Jackson', None, None], ['Ronald Reagan', None, None]])

block1 = Block('Relations',Button('Calculate the selected', table.emit), table)

ename = Edit('Name')

ebirth = Edit('Date of birth', llm = True)

block2 = Block('Person', [ename, Button('Calculate birth date', ebirth.emit)], [ebirth, Edit('Occupation', llm = ename)]) 

blocks = [[block2, block1]]