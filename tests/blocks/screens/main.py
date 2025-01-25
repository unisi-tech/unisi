from unisi import *
from blocks.tblock import config_area, tarea

name = "Blocks"
order = 2

table = Table('Videos', 0, headers = ['Video', 'Duration',  'Links', 'Mine'], rows = [
    ['opt_sync1_3_0.mp4', '30 seconds',  '@Refer to signal1', True],
    ['opt_sync1_3_0.mp4', '37 seconds',  '@Refer to signal8', False]
])

def clear_table(_, value):
    table.rows = []    

clear_button = Button('Clear table', clear_table, icon='swipe')

selector = Select('Mode', 'All', options=['All','Based','Group'])

@handle(selector,'changed')
def selchanged(_, val):
    if val == 'Based':
        return Error('Mode can not be Based!',_)
    _.accept(val)    

def handler(*_):
    tarea.value = ''

clear_text = Button('Clear text', handler, icon='delete')

block = Block('X Block',
    [ clear_text, selector, clear_button],
    [ tarea, table], 
        icon = 'api')

def change_seletion(elem, value):
    for unit in elem.value['nodes']:        
        unit.active = False
    
    for unit in value['nodes']:
        unit.active = True
    elem.value = value

toposcreen = Net('Net', changed = change_seletion )

#graph can handle invalid edges and null nodes in the array    
graph = Graph('_Random graph', 
    nodes = [Node("Node 1"),Node("Node 2", size = 20),None, Node("Node 3", color = "green"), Node("Node 4")],
    edges = [Edge(0,1, color = "#3CA072"), Edge(1,3,'extending', size = 6),Edge(3,4, size = 2), Edge(2,4)])

def switch_graph(*_):    
    bottom_block.value[1] = toposcreen if bottom_block.value[1] == graph else graph

bottom_block = Block('Screen topology: Press Shift for multi (de)select nodes and links', 
     Button('Switch graph', switch_graph), toposcreen, closable = True)

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
