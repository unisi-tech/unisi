# Copyright © 2024 UNISI Tech. All rights reserved.
from .units import *
from .tables import Table
from .containers import Block
from collections import defaultdict
from collections.abc import Iterable

class Node:
    def __init__(self, name , color = '', size = 0, image = ''):
        if name:
            self.name = name
        if image:
            self.type = 'image'
            self.image = image
        else:
            self.type = ''
        if color:
            self.color = color
        if size:
            self.size = size        

class Edge:
    def __init__(self, source, target, name = '', color = '', size = 0, property = None):
        self.source = source
        self.target = target
        if name:
            self.name = name
        if color:
            self.color = color
        if size:
            self.size = size
        if property is not None:
            self.property = property     
    def __str__(self):
        return f"Edge({self.source}->{self.target})"   
    def __repr__(self):
        return f"Edge({self.source}->{self.target})"   

graph_default_value = {'nodes' : [], 'edges' : []}

class Graph(Unit):
    '''has to contain nodes, edges, see Readme'''
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, *args, **kwargs)
        set_defaults(self, dict(type ='graph', value = graph_default_value, nodes = [], edges = []))

Topology = lambda: defaultdict(lambda: defaultdict(lambda: None))

def unit2image(unit):
    match unit:
        case Block():
            return 'https://img.icons8.com/fluency/48/object.png'
        case Button():
            return 'https://img.icons8.com/ios-filled/50/doorbell.png'
        case Edit() | Text():
            return 'https://img.icons8.com/fluency-systems-filled/50/123.png' if unit.type == 'number'\
                else 'https://img.icons8.com/sf-regular/48/abc.png'
        case Switch():
            return 'https://img.icons8.com/ios/50/toggle-on--v1.png'
        case TextArea():
            return 'https://img.icons8.com/color/48/align-cell-content-left.png'
        case Table():
            return 'https://img.icons8.com/color/48/day-view.png' if unit.type == 'table'\
                else 'https://img.icons8.com/ultraviolet/40/combo-chart.png'
        case Tree():
            return 'https://img.icons8.com/external-flatart-icons-outline-flatarticons/64/external-tree-nature-flatart-icons-outline-flatarticons-3.png'
        case Select():
            return 'https://img.icons8.com/cotton/64/list--v2.png'
        case Graph():
            return 'https://img.icons8.com/external-vitaliy-gorbachev-blue-vitaly-gorbachev/50/external-nodes-cryptocurrency-vitaliy-gorbachev-blue-vitaly-gorbachev.png'
        case Range():
            return 'https://img.icons8.com/ios/50/slider-control.png'
        case Unit():
            return 'https://img.icons8.com/ios-filled/50/link--v1.png'
        case _: 
            return ''

class Net(Graph):    
    """Graph of Units"""
    def __init__(self, name, value=graph_default_value, topology=Topology(), **kwargs):        
        Unit.__init__(self, name, **kwargs)        
        self.type = 'graph'         
        self.value = value
        self.topology = topology
        self._build_from_topology()

        changed_handler = getattr(self, 'changed', None)

        def changed_converter(_, value):
            narray = self._narray
            value = dict(
                nodes=[narray[i] for i in value['nodes']], 
                edges=[Edge(narray[self._edges[i].source], narray[self._edges[i].target]) for i in value['edges']]
            )
            if changed_handler:
                return changed_handler(_, value)
            self.value = value

        self.changed = changed_converter

    def _build_from_topology(self):
        nodes, narray, earray = [], [], []
        for sunit, links in self.topology.items():
            sindex = index_of(narray, sunit)
            if sindex == -1:
                sindex = len(narray)
                narray.append(sunit)
                nodes.append(Node(sunit.name, image=unit2image(sunit), color='white', size=15))
            for dunit in links:
                dindex = index_of(narray, dunit)
                if dindex == -1:
                    dindex = len(narray)
                    narray.append(dunit)
                    nodes.append(Node(dunit.name, image=unit2image(dunit), color='white', size=15))
                earray.append(Edge(sindex, dindex))
        self._nodes = nodes
        self._edges = earray
        self._narray = narray

    def elements(self, stubs=True):
        return self._narray

    def __getstate__(self):
        _value = dict(
            nodes=[index_of(self._narray, unit) for unit in self.value['nodes']],
            edges=[Edge(index_of(self._narray, e.source), index_of(self._narray, e.target)) for e in self.value['edges']]
        )
        state = {name: getattr(self, name) for name in self.__dict__
                 if name[0] != '_' and name not in ('topology', 'value')}
        state.update(nodes=self._nodes, edges=self._edges, value=_value)
        return state

    def make_topology(self, unit: Unit | Iterable):
        topo = Topology()
        def dive(unit):
            match unit:
                case Iterable():
                    node = Unit('Union', type='union')
                    for obj in unit:
                        if obj:
                            topo[node][dive(obj)] = {}
                    return node
                case Block():
                    for obj in unit.value:
                        if obj:
                            topo[unit][dive(obj)] = {}
                case _: ...
            return unit
        dive(unit)
        self.topology = topo
        self._build_from_topology()