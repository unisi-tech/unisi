# Copyright © 2024 UNISI Tech. All rights reserved.
from .common import Unishare
from collections import defaultdict

#storage id -> screen name -> [elem name, block name]
dbshare = defaultdict(lambda: defaultdict(lambda: []))
# db id -> [update]
dbupdates = defaultdict(lambda: [])

def at_iter(iter, times):
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

    def get_delta_0(self):
        return self.delta_list[0] if self.cache is None else self.cache[:self.limit]

    def __getstate__(self):        
        return dict(length = len(self),  limit = self.limit, data = self.get_delta_0())
    
    def __getattr__(self, name):
        return self.dbtable.limit if name == 'limit' else None
        
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
           
    def __str__(self):
        return str(self.__getstate__())
        
    def get_delta_chunk(self, index) -> tuple[int, list]:
        """return delta list and chunk of data"""
        if index >= len(self):
            return -1, None
        delta_list = index // self.limit * self.limit
        
        if self.cache is not None:
            return delta_list, self.cache[delta_list:delta_list + self.limit]
        
        lst = self.delta_list.get(delta_list)
        if lst is not None:
            return delta_list, lst
        lst = self.dbtable.read_rows(skip = delta_list)
        self.delta_list[delta_list] = lst        
        return delta_list, lst

    def __getitem__(self, index) -> list:
        """return row from delta list or cache"""
        if self.cache is not None:
            return self.cache[index]
        delta_list, chunk = self.get_delta_chunk(index)
        if chunk:
            return chunk[index - delta_list]        

    def __setitem__(self, index, value: list):
        """update row in delta list or cache"""
        if self.cache is not None:
            self.cache[index] = value
        else:
            delta_list, chunk = self.get_delta_chunk(index)
            if chunk:
                chunk[index - delta_list] = value            
            self.dbtable.assign_row(value)
            update = dict(type = 'action', update = 'update', index = index, data = value)
            dbupdates[self.dbtable.id].append(update)            

    def clean_cache_from(self, delta_list):
        """clear dirty delta_list cache"""
        self.delta_list = {k: v for k, v in self.delta_list.items() if k < delta_list}                
        
    def __delitem__(self, index):
        delta_list, chunk = self.get_delta_chunk(index)
        if chunk:
            self.dbtable.delete_row(index)
            update = dict(type = 'action',update ='delete', index = index, exclude = True)            
            dbupdates[self.dbtable.id].append(update)
            del chunk[index - delta_list] 
            limit = self.dbtable.limit
            next_delta_list = delta_list + limit
            if len(chunk) == limit - 1: #chunk was fully filled                                
                next_list = self.delta_list.get(next_delta_list)            
                if next_list:
                    chunk.append(next_list[0])                                                                               
                self.clean_cache_from(next_delta_list)            

    def __len__(self):
        return len(self.cache) if self.cache is not None else self.dbtable.length
    
    def index2node_relation(self, cell_index):
        """calculate delta to property of node or link for persistent"""
        table_fields = self.dbtable.table_fields
        delta = cell_index - len(table_fields)       
        if delta < 0:            
            return True, at_iter(table_fields, cell_index)
        delta -= 1 #ID field
        return False, at_iter(self.dbtable.list.link[1], delta)
    
    def update_cell(self, delta, cell, value, id = None) -> dict:
        in_node, field = self.index2node_relation(cell)                    
        if in_node:
            table_id = self.dbtable.id 
            row_id =  self[delta][len(self.dbtable.table_fields)] 
        else:
            table_id = self.dbtable.list.link[2]
            row_id = id
        self.dbtable.db.update_row(table_id, row_id, {field: value}, in_node) 
        self[delta][cell] = value
        if self.cache is None: 
            update = dict(type = 'action', update = 'update', index = delta, data = self[delta])
            dbupdates[self.dbtable.id].append(update)        
            return update

    def append(self, arr):
        """append row to list"""
        if self.cache is not None:
            self.cache.append(arr)
            return arr
        index = len(self)
        row = self.dbtable.append_row(arr)                    
        delta_chunk,list = self.get_delta_chunk(index)
        if list is not None:
            list.append(row)
            update = dict(type = 'action', update = 'add', index = index, data = row) 
            dbupdates[self.dbtable.id].append(update)
            return row
        
    def extend(self, rows) -> dict:
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
        update = dict(type = 'action', update = 'updates', index = delta, data = data, length = length)
        dbupdates[self.dbtable.id].append(update)        
    
    def insert(self, index, value):
        self.append(value)        

    def remove(self, value):
        index = value[-1]
        del self[index]

    def pop(self, index = -1):
        value = self[index]
        del self[index]
        return value

    def clear(self, detach = False):
        self.dbtable.clear(detach)
        self.delta_list = {0: None}
        dbupdates[self.dbtable.id].append(dict(type = 'action', update = 'updates', length = 0))        
