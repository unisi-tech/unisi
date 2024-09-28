from .units import *
from .tables import Table
from .common import pretty4, flatten
from numbers import Number
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

Topology = lambda: defaultdict(lambda: defaultdict(lambda: {}))

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
    def __init__(self, name, topology = Topology(),  *args, **kwargs):        
        super().__init__(name, *args, **kwargs)
        self.type = 'graph' 
        self.topology = topology       
        changed_handler = getattr(self, 'changed', None)
        
        def changed_converter(_, value):            
            mark_changed = self._mark_changed 
            self._mark_changed = None #turn off for 'value' diff  reaction
            self._value = value
            narray = self._narray
            value = dict(nodes = [self._narray[i] for i in value['nodes']], edges = 
                [Edge(narray[self._edges[i].source], narray[self._edges[i].target]) for i in value['edges']])
            if changed_handler:
                result = changed_handler(_, value)
            else:
                result = None
                self.value = value
            self._mark_changed = mark_changed #turn on
            return result
        self.changed = changed_converter

    def specific_changed_register(self, property = None, value = None):
        """ mark serial info as invalid """
        if property:
            if property.startswith('_'):
                return False
            else:
                self.delattr('_nodes')
                self.delattr('_value')                            
        return True

    def elements(self, stubs=True):
        if not hasattr(self, '_nodes'):
            self.__getstate__()
        return self.narray
    
    def __getstate__(self):
        if not hasattr(self, '_nodes'):
            nodes = []            
            narray = []
            earray = []
            for sunit, links in self.topology.items():
                sindex = index_of(narray,sunit)
                if sindex == -1:
                    sindex = len(narray)
                    narray.append(sunit)
                    nodes.append(Node(sunit.name, image = unit2image(sunit), 
                            color = 'white', size = 15))
                for dunit  in links:
                    dindex = index_of(narray,dunit)
                    if dindex == -1:
                        dindex = len(narray)
                        narray.append(dunit)
                        nodes.append(Node(dunit.name, image = unit2image(dunit), 
                            color = 'white', size = 15))
                    earray.append(Edge(sindex, dindex))
            self._nodes = nodes
            self._edges = earray
            self._narray = narray            

        if not hasattr(self, '_value'):
            self._value = dict(nodes = [index_of(self._narray,unit) for unit in self.value['nodes']], 
            edges = [Edge(index_of(self._narray, e.source), index_of(self._narray, e.target)) for e in self.value['edges']])
        return dict(name = self.name, type = self.type, nodes = self._nodes, edges = self._edges, value = self._value)

    def make_topology(self, unit: Unit | Iterable):
        topo = Topology()
        def dive(unit):
            match unit:
                case Iterable():
                    node = Unit('Union', type = 'union')            
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
        self.specific_changed_register()

class ContentScaler(Range):
    def __init__(self, *args, **kwargs):
        name = args[0] if args else 'Scale content'        
        super().__init__(name, *args, **kwargs)                
        if 'options' not in kwargs:
            self.options = [0.25, 3.0, 0.25]
        self.changed = self.scaler
        
    def scaler(self, _, val):
        prev = self.value
        elements = self.elements() 
        self.value = val
        if elements:
            prev /= val
            for element in elements:
                element.width /= prev
                element.height /= prev            
            return elements
        
class Block(Unit):    
    def __init__(self, name, *elems, **options):    
        self._mark_changed = None        
        self.name = name        
        self.type = 'block'
        self.value = list(elems)        
        self.add(options)  
        if getattr(self,'scaler', False):
            scaler = ContentScaler(elements = lambda: self.scroll_list)
            self.scaler = scaler
            if not self.value:
                self.value = [[scaler]]
            elif isinstance(self.value[0], list):
                self.value[0].append(scaler)
            else:
                self.value[0] = [self.value, scaler]     

        for elem in flatten(self.value):                        
            if hasattr(elem, 'llm'): 
                if elem.llm is True:
                   dependencies = [obj for obj in flatten(self.value) if elem is not obj and obj.type != 'command'] 
                   exactly = False                               
                elif isinstance(elem.llm, list | tuple):
                    dependencies = elem.llm                    
                    exactly = True
                elif isinstance(elem.llm, Unit):                    
                    dependencies = [elem.llm]
                    exactly = True
                elif isinstance(elem.llm, dict):
                    if elem.type != 'table':                        
                        raise AttributeError(f'{elem.name} llm parameter is a dictionary only for tables, not for {elem.type}!')                                                                
                    elem.__llm_dependencies__ = {fld: (deps if isinstance(deps, list | bool) else [deps]) for fld, deps in elem.llm.items()} 
                    elem.llm = True
                    continue
                else:
                    raise AttributeError(f'Invalid llm parameter value for {elem.name} {elem.type}!')
                if dependencies:
                    elem.llm = exactly                
                    for dependency in dependencies:
                        dependency.add_changed_handler(elem.emit)    
                    elem.__llm_dependencies__ = dependencies
                else:
                    elem.llm = None
                    print(f'Empty dependency list for llm calculation for {elem.name} {elem.type}!')
        
        self.set_reactivity(Unishare.context_user())

    def set_reactivity(self, user, override = False):
        if user:            
            super().set_reactivity(user, override)
            for elem in flatten(self.value):
                elem.set_reactivity(user)
                
    @property
    def compact_view(self) -> str:
        return ','.join(obj.compact_view for obj in flatten(self.value) if obj.value)

    @property
    def scroll_list(self):            
        return self.value[1] if len(self.value) > 1 and isinstance(self.value[1], (list, tuple)) else []
    
    @scroll_list.setter
    def scroll_list(self, lst):
        self.value = [self.value[0] if self.value else [], lst]
        if hasattr(self,'scaler'):
            sval = self.scaler.value
            if sval != 1:
                self.scaler.value = 1
                self.scaler.changed(self.scaler, sval)        
        self.set_reactivity(Unishare.context_user())

class ParamBlock(Block):
    def __init__(self, name, *args, row = 3, **params):
        """ does not need reactivity so Block init is not used"""
        self._mark_changed = None
        if not args:
            args = [[]]
        self._mark_changed = None        
        self.name = name        
        self.type = 'block'
        self.value = list(args)
        self.name2elem = {}
        cnt = 0        

        for param, val in params.items():                    
            pretty_name = pretty4(param)            
            t = type(val)
            if t == str or t == int or t == float:
                el = Edit(pretty_name, val)
            elif t == bool:
                el = Switch(pretty_name, val)
            elif t == tuple or t == list:
                if len(val) != 2:
                    raise ValueError('Composite value has to contain the current value and options value!')
                options = val[1]
                if not isinstance(options, list | tuple | dict):
                    raise ValueError('Options value (the second parameter) has to be a list or tuple!')
                if len(options) == 3 and all(map(lambda e: isinstance(e, Number), options)):
                    el = Range(pretty_name, val[0], options = options)
                elif isinstance(options, list | tuple):
                    el = Select(pretty_name, val[0], options = options, type = 'select')
                else: 
                    el = Tree(pretty_name, val[0], options = options)
            else:
                raise ValueError(f'The {param} value {val} is not supported. Look at ParamBlock documentation!')
            
            self.name2elem[param] = el
            
            if cnt % row == 0:
                block = []
                self.value.append(block)
            cnt += 1
            block.append(el)
    @property
    def params(self):
        return {name: el.value for name, el in self.name2elem.items()}

class Dialog:  
    def __init__(self, question, callback, *content, commands = ['Ok','Cancel'],
            icon = 'not_listed_location'):        
        self.type = 'dialog'         
        self.name = question
        self.changed = callback          
        self.commands = commands
        self.icon = icon
        self.value = [[], *content] if content else []        

class Screen:
    def __init__(self, name, **kwargs):
        self.name = name        
        self.__dict__.update(kwargs) 
        self.type = 'screen'

