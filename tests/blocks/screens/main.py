from unisi import *
from blocks.tblock import config_area, tarea, changed

name = "Main"
order = 1

def append_row(table, value):
    row = [''] * 4
    row[-1] = False
    table.rows.append(row)
    return row #new row

table = Table('Videos', 0, headers = ['Video', 'Duration',  'Links', 'Mine'], rows = [
    ['opt_sync1_3_0.mp4', '30 seconds',  '@Refer to signal1', True],
    ['opt_sync1_3_0.mp4', '37 seconds',  '@Refer to signal8', False]
], append = append_row, delete = delete_table_row)

def clean_table(_, value):
    table.rows = []    

clean_button= Button('Clean table', clean_table, icon='swipe')

selector = Select('Select', 'All', options=['All','Based','Group'])

@handle(selector,'changed')
def selchanged(_, val):
    if val == 'Based':
        return Error('Select can not be Based!',_)
    _.accept(val)    

def replace_image(_, iname):
    print(iname)    

block = Block('X Block',
    [           
        clean_button,
        selector
    ],
    [
        tarea, table
    ], icon = 'api')

def delblock(elem, value):
    context_screen().blocks = [block, config_area]
    return Redesign

toposcreen = Net('Net', changed = changed )

bottom_block = Block('Screen topology: Press Shift for multi (de)select nodes and links', 
     Button('Delete block', delblock), toposcreen)

blocks= [[block,bottom_block],config_area]

async def log(x,y):    
    user = context_user()
    for i in range(3):
        await user.send(Warning(str(i)))
    
toolbar = [Button('_Save', log, icon = 'save', tooltip = 'Save info'),
        Button('_Ignored', lambda *_: Info('ignored!'), icon = 'delete_forever', tooltip = 'Ignore info!')]

def prepare():
    if not toposcreen.topology:
        toposcreen.make_topology(blocks)
