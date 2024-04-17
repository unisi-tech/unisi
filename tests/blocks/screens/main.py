from unisi import *
from blocks.tblock import config_area, tarea

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
    return table

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
    ], [tarea, table], icon = 'api')

import random

def add_node(_, v):
    i = len(graph.nodes)
    name = f'node{i}'    
    source = random.randrange(i)
    if graph.nodes[source] is None:
        source = 1
    graph.edges.append(Edge(source, i ))
    graph.nodes.append(Node(name))
    return graph

def graph_selection(_, val):
    _.value = val    
    return Info(f'Nodes {val["nodes"]}, Edges {val["edges"]}') 

#graph can handle invalid edges and null nodes in the array    
graph = Graph('test graph', None, graph_selection, 
    nodes = [Node("Node 1"),Node("Node 2", size = 20),None, Node("Node 3", color = "green"), Node("Node 4")],
    edges = [Edge(0,1, color = "#3CA072"), Edge(1,3,'extending', size = 6),Edge(3,4, size = 2), Edge(2,4)])

def delblock(elem, value):
    screen.blocks = [block, config_area]
    return Redesign

bottom_block = Block('Graph, press Shift for multi (de)select', 
    [Button('Add node', add_node),  Button('Delete block', delblock)], 
    graph)

blocks= [[block,bottom_block],config_area]

async def log(x,y):    
    for i in range(3):
        await user.send(Warning(str(i)))
    
toolbar = [Button('_Save', log, icon = 'save', tooltip = 'Save info'),
        Button('_Ignored', lambda _, x: Info('ignored!'), icon = 'delete_forever', tooltip = 'Ignore info!')]
