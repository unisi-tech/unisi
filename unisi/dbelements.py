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
        
    """ The methods causes invalid serialization!
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
            while len(chunk) == limit - 1: #chunk was fully filled                                
                next_list = self.delta_list.get(next_delta_list)            
                if next_list:
                    chunk.append(next_list[0])                        
                    chunk = next_list
                    next_delta_list += limit
                    del next_list[0]
                else:
                    last = self.dbtable.read_rows(skip = next_delta_list - 1, limit = 1)[0]
                    chunk.append(last)                     
                    #clean dictionary from following elements
                    self.clean_cache_from(next_delta_list)
                    break            

    def __len__(self):
        return len(self.cache) if self.cache is not None else self.dbtable.length

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
        start = self.dbtable.length
        rows = self.dbtable.append_rows(rows)
        len_rows = len(rows)
        delta_list_update = start
        
        i_rows = 0
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
        self.update = self.dbtable.get_init_list().update

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
