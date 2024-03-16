from unisi import *
import random, copy, time

def append(_,val):
    ''' append has to return new row or error string'''
    id, search = val
    new = [search, '', '']
    _.rows.append(new)
    return new

def updated(_, value):
    _.value = value  
    return Info(f'{_.name} is updated to {value}!')        

def complete(_, value):
    value = value[0]
    if value and isinstance(value, str) and len(value) > 2 and value[0].isalpha():
        return ['aaa', 'bbbb', 'cccccc']
    return []

def changed(_, value):
    _.value = value  
    return Warning(f'{_.name} changed to {value}!')

def table_modify(_, value):
    value, pos = value    
    return Error(f'{_.name} {pos} is not modified to value {value}!')

def table_update(_, value):    
    accept_cell_value(_, value)
    value, pos = value        
    return Info(f'{_.name} {pos} is updated to value {value}!')

ticks = Edit('How many ticks', 50)

def dialog_callback(_,value):    
    if value == 'Ok':
        user.progress('Process executing')
        for i in range(ticks.value):
            txt.value = i
            user.progress(f'{i} tick', txt)
            time.sleep(0.04)
        return user.progress(None)

def call_dialog(*_):
    return Dialog('Start a long process?', dialog_callback, ticks)

table = Table('Audios', 0, changed, type = 'chart', headers = ['Audio', 'Duration,sec', 'Stars'], multimode = True,
    rows =  [[f'sync{i}.mp3', round(random.random() * 15000) / 100, random.randint(1,50)] for i in range(100)],
    append = append, complete = complete, update = table_update, view = 'i-1,2')

tarea = TextArea('Area', "Alpha Centauri A, also known as Rigil Kentaurus, is the principal member, or primary, of the binary system. It is a solar-like main-sequence star with a similar yellowish colour, whose stellar classification is spectral type G2-V; it is about 10% more massive than the Sun, with a radius about 22% larger.",
            update = updated)

cloned_table_typed = copy.copy(table)
cloned_table_typed.type = 'table'

ld = { 
    'Animals' : None,
    'Brushtail Possum' : 'Animals',
    'Genet' : 'Animals',
    'Silky Anteater' : 'Animals',
    'Greater Glider' : 'Animals',
    'Tarsier' : 'Animals',
    'Kinkajou' : 'Animals',
    'Tree Kangaroo' : 'Animals',
    'Sunda Flying Lemur' : 'Animals',
    'Green Tree Python' : 'Animals',
    'Fruit Bat' : 'Animals',
    'Tree Porcupines' : 'Animals',
    'Small Tarsier' : 'Tarsier',
    'Very small Tarsier': 'Small Tarsier'
}

def tree_changed(_, item): 
    _.value = item 
    return Info(f"{item} selected in the tree.")

tree = Tree('_Inheritance','Animals', tree_changed, options = ld)

txt = Text('Text about cats')

simple_enter = Edit('_Simple Enter update', 'cherokke', update = updated)

list_complete = ['value 21', 'Value 21', 'sum 289', 'absolute 3']

eblock = Block('New block',                        
        [Button('Dialog for a process', call_dialog), Range('Scaling', 0, lambda _,val: Info(val), options=[0,1,0.1])],
        txt,
        Edit('Number only', 2.5),
        Edit('Edit string', 'xyz'),
        Edit('Complete enter update field', '', changed, 
            complete = smart_complete(list_complete), update = updated)
)

def add_tree_elem(_, val):
    txt = simple_enter.value
    if not txt:
        return Info('Enter text first to the field!')
    if txt in ld:
        return Warning('Cannot add doubles!')
    
    ld[txt] = tree.value if tree.value else None
    tree.value = txt
    return tree

treeblock = Block('Tree block',[simple_enter, Button('_Add to tree', add_tree_elem, icon='add_circle')], tree, icon = 'account_tree')

tableblock = Block('Table block', [], table, icon = 'insights')

config_area = [eblock, [treeblock, tableblock]]

