from .common import Unishare
import asyncio
from collections import defaultdict

#storage id -> screen name -> [elem name, block name]
dbshare = defaultdict(lambda: defaultdict(lambda: []))
# (db id, exclude user from updating) -> update
dbupdates = defaultdict(lambda: [])

async def sync_dbupdates():
    sync_calls = []
    for (id, exclude), updates in dbupdates.items():
        for update in updates:
            screen2el_bl = dbshare[id]
            for user in Unishare.sessions.values():                
                if user is not exclude:
                    scr_name = user.screen.name
                    if scr_name in screen2el_bl:
                        for elem_block in screen2el_bl[scr_name]: 
                            update4user = {**update, **elem_block}
                            sync_calls.append(user.send(update4user))
    dbupdates.clear()
    await asyncio.gather(*sync_calls)

def iterate(iter, times):
    for i, val in enumerate(iter):
        if i == times:
            return val
    
class Dblist:
    def __init__(self, dbtable, init_list = None, cache = None):                
        self.cache = cache
        self.limit = dbtable.limit
        if cache is not None:            
            init_list = cache[:self.limit]      
        elif init_list is None:
            raise AttributeError('init_list or cache has to be assigned!')
                              
        self.delta_list = {0 : init_list}                
        self.dbtable = dbtable                
        self.update = dict(type ='init', length = len(self), 
                limit = self.limit, data = init_list) 

    def get_delta_0(self):
        return self.delta_list[0]

    def __getattribute__(self, name):
        if name == '__dict__':
            return object.__getattribute__(self, 'update')
        return object.__getattribute__(self, name)
    
    def __getattr__(self, name):
        return self.dbtable.limit if name == 'limit' else None
        
    """ The methods causes invalid serialization in Python and not used!
    def __iter__(self):
        "Override the default iterator to provide custom behavior."
        self._index = 0
        return self

    def __next__(self):        
        if self._index < len(self):
            value = self[self._index]
            self._index += 1
            return value
        else:
            raise StopIteration
    """
    
    def __str__(self):
        return f'\ndeltas: {self.delta_list}\nupdate: {self.update}'        
        
    def get_delta_chunk(self, index):
        if index >= len(self):
            return -1, None
        delta_list = index // self.limit * self.limit
        
        if self.cache is not None:
            return delta_list, self.cache[delta_list:delta_list + self.limit]
        
        lst = self.delta_list.get(delta_list)
        if lst:
            return delta_list, lst
        lst = self.dbtable.read_rows(skip = delta_list)
        self.delta_list[delta_list] = lst        
        return delta_list, lst

    def __getitem__(self, index):        
        if self.cache is not None:
            return self.cache[index]
        delta_list, chunk = self.get_delta_chunk(index)
        if chunk:
            return chunk[index - delta_list]        

    def __setitem__(self, index, value):
        if self.cache is not None:
            self.cache[index] = value
        else:
            delta_list, chunk = self.get_delta_chunk(index)
            if chunk:
                chunk[index - delta_list] = value
            self.update = dict(type = 'update', index = index, data = value)
            self.dbtable.assign_row(value)

    def clean_cache_from(self, delta_list):
        """clear dirty delta_list cache"""
        self.delta_list = {k: v for k, v in self.delta_list.items() if k < delta_list}                
        
    def __delitem__(self, index):
        delta_list, chunk = self.get_delta_chunk(index)
        if chunk:
            self.dbtable.delete_row(index)
            self.update = dict(type ='delete', index = index)            
            del chunk[index - delta_list] 
            limit = self.dbtable.limit
            next_delta_list = delta_list + limit
            if len(chunk) == limit - 1: #chunk was fully filled                                
                next_list = self.delta_list.get(next_delta_list)            
                if next_list:
                    chunk.append(next_list[0])                                                               
                else:
                    delta_list, chunk = self.get_delta_chunk(delta_list)                    
                    self.update = dict(type = 'updates', index = delta_list, data = chunk)
                self.clean_cache_from(next_delta_list)                     

    def __len__(self):
        return len(self.cache) if self.cache is not None else self.dbtable.length
    
    def index2node_relation(self, cell_index):
        """calculate delta to property of node or link for persistent"""
        table_fields = self.dbtable.table_fields
        delta = cell_index - len(table_fields)       
        if delta < 0:            
            return True, iterate(table_fields, cell_index)
        delta -= 1 #ID field
        return False, iterate(self.link, delta)
    
    def update_cell(self, delta, cell, value, id = None):
        in_node, field = self.index2node_relation(cell)                    
        if in_node:
            table_id = self.dbtable.id 
            row_id =  self[delta][len(self.dbtable.table_fields)] 
        else:
            table_id = self.link[2]
            row_id = id
        self.dbtable.db.update_row(table_id, row_id, {field: value}, in_node) 
        self[delta][cell] = value 
        delta, data = self.get_delta_chunk(delta)
        self.update = dict(type = 'updates', index = delta, data = data)
        return self.update 

    def append(self, value):
        if self.cache is not None:
            self.cache.append(value)
            return value[-1]
        index = len(self)
        id = self.dbtable.append_row(value)            
        delta_list = index // self.limit * self.limit
        list = self.delta_list.get(delta_list)
        if list:
            list.append(value)
            self.update = dict(type = 'add', index = index, data = value) 
        return id
        
    def extend(self, rows):
        delta_start = self.dbtable.length
        start = delta_start
        rows = self.dbtable.append_rows(rows)
        len_rows = len(rows)                
        i_rows = 0
        length = len_rows + start
        while len_rows > 0:
            delta_list = start // self.limit * self.limit
            list = self.delta_list.get(delta_list)
            if list is None:            
                list = []
                self.delta_list[delta_list] = list
                can_fill = self.limit
            else:
                can_fill = self.limit - len(list)
            if can_fill:        
                list.extend(rows[i_rows: i_rows + can_fill])                
                        
            i_rows += can_fill           
            start += can_fill
            len_rows -= can_fill        
        delta, data = self.get_delta_chunk(delta_start)
        self.update = dict(type = 'updates', index = delta, data = data, length = length)
        return self.update

    def insert(self, index, value):
        self.append(value)        

    def remove(self, value):
        index = value[-1]
        del self[index]

    def pop(self, index = -1):
        value = self[index]
        del self[index]
        return value

    def clear(self):
        self.dbtable.clear()
