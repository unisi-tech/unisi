from .units import Unit
from .common import *
from .dbunits import Dblist
from .llmrag import get_property
import asyncio

relation_mark = 'Ⓡ'
exclude_mark = '✘'
max_len_rows4llm = 30

def get_chunk(obj, start_index):
    delta, data = obj.rows.get_delta_chunk(start_index)
    return {'type': 'updates', 'index': delta, 'data': data}

def iterate(iter, times):
    for i, val in enumerate(iter):
        if i == times:
            return val

def accept_cell_value(table, dval):        
    value = dval['value']
    if not isinstance(value, bool):
        try:
            value = float(value)        
        except:
            pass            
    if hasattr(table,'id'):
        dbt = table.rows.dbtable
        in_node, field = table.index2node_relation(dval['cell'])                    
        if in_node:
            table_id = table.id 
            row_id =  table.rows[dval['delta']][len(dbt.table_fields)] 
        else:
            table_id = table.__link__[2]
            row_id = dval['id']             
        dbt.db.update_row(table_id, row_id, {field: value}, in_node)    
    table.rows[dval['delta']][dval['cell']] = value    
            
def delete_table_row(table, value):    
    if table.selected_list:
        if hasattr(table, 'link') and table.filter:
            link_table, rel_props, rel_name = table.__link__        
            if not isinstance(value, list):                                
                value = [value]
            table.rows.dbtable.delete_links(link_table.id, link_ids = value, index_name = rel_name)
            table.__link_table_selection_changed__(link_table, link_table.value)
            return table
        elif isinstance(value, list):                    
            value.sort(reverse = True)
            for v in value:            
                del table.rows[v]
            table.value = []
        else:            
            del table.rows[value]  
            table.value = None    

def append_table_row(table, search_str):
    ''' append has to return new row, value is the search string value in the table'''    
    new_row = [None] * len(table.rows.dbtable.table_fields)           
    if getattr(table,'id', None):          
        id = table.rows.dbtable.list.append(new_row)
        new_row.append(id)         
        if hasattr(table, 'link') and table.filter:
            link_table, _, rel_name = table.__link__               
            for linked_id in link_table.selected_list:
                relation = table.rows.dbtable.add_link(id, link_table.id, linked_id, link_index_name = rel_name) 
                new_row.extend(relation)                     
                break                 
    table.rows.append(new_row)
    return new_row

class Table(Unit):
    def __init__(self, *args, panda = None, **kwargs):
        if panda is not None:
            self.mutate(PandaTable(*args, panda=panda, **kwargs))
        else:
            super().__init__(*args, **kwargs)    
            set_defaults(self, dict(headers = [], type = 'table', value = None, rows = [], editing = False, dense = True))
            self.__headers__ = self.headers[:]
        if getattr(self,'id', None): 
            db = Unishare.context_user().db
            if db:
                db.set_db_list(self)
            else:
                raise AssertionError('Config db_dir is not defined!')            
            self.get = get_chunk
            has_link = hasattr(self, 'link')
            set_defaults(self, {'filter': has_link, 'ids': False, 'search': ''})
            if has_link: 
                prop_types = {}
                rel_name = ''                
                match self.link:
                    case [link_table, prop_types, rel_name]: ...
                    case [link_table, prop_types]: ...
                    case link_table: ...
                rel_name, rel_fields = self.rows.dbtable.get_rel_fields2(link_table.id, prop_types, rel_name)
                if not hasattr(link_table, 'id'):
                    raise AttributeError('Linked table has to be persistent!')
                self.__link__ = link_table, list(prop_types.keys()), rel_name
                self.link = rel_fields
                
                @Unishare.handle(link_table,'changed')
                def link_table_selection_changed(master_table, val, init = False):
                    lstvalue = val if isinstance(val, list) else [val] if val != None else []
                    if lstvalue:
                        link_ids = [link_table.rows[val][-1] for val in lstvalue]
                        link_rows = self.rows.dbtable.calc_linked_rows(rel_name, link_ids, self.filter, self.search)
                    else:
                        link_rows = Dblist(self.rows.dbtable, cache = [])
                    if self.filter:                    
                        self.clean_selection()
                        self.rows = link_rows
                    else: 
                        selected_ids = [link_rows[i][-1] for i in range(len(link_rows))]
                        self.value = selected_ids
                        #restore table rows if they are not rows
                        if self.rows.cache is not None:
                           self.rows = self.rows.dbtable.get_init_list()
                    if not init:
                        master_table.accept(val)              
                        return self     
                link_table_selection_changed(link_table, link_table.value, True)
                self.__link_table_selection_changed__ = link_table_selection_changed

                @Unishare.handle(self,'filter')
                def filter_status_changed(table, value):
                    self.filter = value
                    link_table_selection_changed(link_table, link_table.value, True)
                    self.calc_headers()
                    return self                
                
                @Unishare.handle(self,'changed')
                def changed_selection_causes__changing_links(self, new_value):                   
                    if link_table.value is not None and link_table.value != []:
                        #if link table is in multi mode, links are not editable 
                        if not self.filter and not isinstance(link_table.value, list | tuple):
                            if  self.editing:
                                actual = set(new_value if isinstance(new_value, list) else [] if new_value is None else [new_value])
                                old = set(self.value if isinstance(self.value, list) else ([] if self.value is None else [self.value]))                        
                                deselected = old - actual                        
                                if deselected:
                                    self.rows.dbtable.delete_links(link_table.id, link_table.value, deselected)                            
                                selected = actual - old
                                if selected:
                                    self.rows.dbtable.add_links(link_table.id, selected, link_table.value)                                                        
                            else:
                                return Warning('The linked table is not in edit mode', self)
                    return self.accept(new_value)    
                
            @Unishare.handle(self,'search')
            def search_changed(table, value):
                self.search = value
                if has_link:
                    link_table_selection_changed(link_table, link_table.value, True)
                else:
                    self.rows = self.rows.dbtable.get_init_list(self.search)
                return self
                
            self.calc_headers()                                
                    
        elif hasattr(self,'ids'):
            raise ValueError("Only persistent tables can have 'ids' option!")

        if getattr(self,'edit', True): 
            set_defaults(self,{'delete': delete_table_row, 'append': append_table_row, 'modify': accept_cell_value})   

    @property
    def compact_view(self):
        """only selected are sended to llm"""
        selected = self.selected_list
        result = []
        if not selected and len(self.rows) < max_len_rows4llm:
            selected = range(len(self.rows))
        for index in selected:
            result.append({field: value for field, value in zip(self.headers, self.rows[index])})
        return {'name': self.name, 'value': result}          
    
    @property
    def selected_list(self):                            
        return [] if self.value is None else self.value if isinstance(self.value, list) else [self.value]   

    def clean_selection(self):        
        self.value = [] if isinstance(self.value,tuple | list) else None
        return self
    
    def calc_headers(self):        
        """only for persistent"""
        table_fields = self.rows.dbtable.table_fields
        self.headers = self.__headers__[:] if self.__headers__ else [pretty4(prop)for prop in table_fields]
        only_node_headers = len(self.headers) == len(table_fields)
        if self.ids:
            self.headers.insert(len(table_fields), 'ID')
        elif self.filter:
            self.headers.insert(len(table_fields), exclude_mark + 'ID')
        if self.filter:
            if only_node_headers:
                self.headers.extend([relation_mark + pretty4(link_field) for link_field in self.link])
            if self.ids:
                self.headers.append(relation_mark + 'ID')

    def index2node_relation(self, cell_index):
        """calculate delta to property of node or link for persistent"""
        table_fields = self.rows.dbtable.table_fields
        delta = cell_index - len(table_fields)       
        if delta < 0:            
            return True, iterate(table_fields, cell_index)
        delta -= 1 #ID field
        return False, iterate(self.link, delta)
    
    async def emit(self, *_):        
        """calcute llm field values for selected rows if they are None"""        
        if Unishare.llm_model and getattr(self, 'llm', None) is not None:              
            tasks = []
            for index in self.selected_list:
                values = {field: value for field, value in zip(self.headers, self.rows[index]) 
                          if value is not None and value != ''}
                for fld, deps in self.__llm_dependencies__.items():                    
                    if fld not in values:
                        context = {}
                        for dep in deps:
                            value = values.get(dep, None)
                            if value is None:
                                if self.llm: #exact
                                    return   #not all fields
                            else:                                
                                if isinstance(dep, str):
                                    context[dep] = value
                                elif isinstance(dep, Unit):
                                    context[dep.name] = dep.value                                    
                                else:
                                    raise AttributeError(f'Invalid llm parameter {dep} in {self.name} element!')
                        if context:                                                    
                            async def assign(index, fld, jcontext):
                                self.rows[index][self.headers.index(fld)] = await get_property(fld, jcontext)
                            tasks.append(asyncio.create_task(assign(index, fld, toJson(context))))
            if tasks:
                await asyncio.gather(*tasks)
                return self
        
def delete_panda_row(table, row_num):    
    df = table.__panda__
    if row_num < 0 or row_num >= len(df):
        raise ValueError("Row number is out of range")
    pt = table.__panda__
    pt.drop(index = row_num,  inplace=True)
    pt.reset_index(inplace=True) 
    delete_table_row(table, row_num)    

def accept_panda_cell(table, value_pos):
    value, position = value_pos
    row_num, col_num = position
    table.__panda__.iloc[row_num,col_num] = value
    accept_cell_value(table, value_pos)

def append_panda_row(table, row_num):    
    df = table.__panda__
    new_row = append_table_row(table, row_num)
    df.loc[len(df), df.columns] = new_row
    return new_row    

class PandaTable(Table):
    """ panda = opened panda table"""
    def __init__(self, *args, panda = None, fix_headers = True, **kwargs):
        super().__init__(*args, **kwargs)                
        if panda is None:
            raise Exception('PandaTable has to get panda = pandaTable as an argument.')
        self.headers = panda.columns.tolist()
        if fix_headers:
            self.headers = [pretty4(header) for header in self.headers]        
        self.rows = panda.values.tolist()
        self.__panda__ = panda

        if getattr(self,'edit', True): 
            set_defaults(self,{'delete': delete_panda_row, 'append': append_panda_row,
                'modify': accept_panda_cell})
    @property
    def panda(self):
        return getattr(self,'__panda__',None) 
    