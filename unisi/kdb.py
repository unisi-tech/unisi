# Copyright © 2024 UNISI Tech. All rights reserved.
import kuzu, shutil, os, re
from datetime import date, datetime
from cymple import QueryBuilder as qb
from cymple.typedefs import Properties
from .common import get_default_args
from .dbunits import Dblist

def equal_fields_dicts(dict1, dict2):
    return dict1.keys() == dict2.keys() and all(dict1[key].lower() == dict2[key].lower() for key in dict1)

def is_modifying_query(cypher_query):
    query = cypher_query.lower()
    modifying_pattern = r'\b(create|delete|detach\s+delete|set|merge|remove|call\s+\w+\s+yield|foreach)\b'        
    return re.search(modifying_pattern, query)

def query_offset(id, offset):
    return qb().match().node(id, 'a').where('a.ID','=',offset)  

def kuzu_data_type(value):
    match value:
        case bool():
            return "BOOLEAN"
        case int():        
            return "INT64"
        case float():
            return "DOUBLE"
        case str():
            return "STRING"
        case datetime():
            return "TIMESTAMP"        
        case date():
            return "DATE"
        case bytes():
            return "BLOB"          
        case list() | tuple():
            return "LIST"
        case _:
            return ""

number_types = ["DOUBLE", "INT64"]

def dict_to_cypher_set(properties, alias = 'a'):
    set_items = []
    for key, value in properties.items():
        if isinstance(value, str):    
            set_items.append(f"{alias}.{key} = '{value}'")
        else:
            set_items.append(f"{alias}.{key} = {value}")        
    return "SET " + ", ".join(set_items)

class Database:
    tables = {} #id -> Dbtable
    def __init__(self, dbpath, message_logger = print) -> None:    
        self.db = kuzu.Database(dbpath)
        self.conn = kuzu.Connection(self.db)
        self.message_logger = message_logger
        self.table_params = get_default_args(self.get_table)
    
    def execute(self, query_str, ignore_exception = False):
        query_str = str(query_str)
        """ if not query_str.endswith(';'):
            query_str += ';'
        print(query_str) """
        try:
            result = self.conn.execute(query_str)            
        except Exception as e:
            if not ignore_exception:
                self.message_logger(e)
            return None
        return True if result is None else result            
        
    def delete(dir_path):
        if os.path.exists(dir_path):
            # Remove the directory and all its contents
            shutil.rmtree(dir_path)            

    @property
    def table_names(self):
        return self.conn._get_node_table_names()
    
    def get_table_fields(self, table_name, remove_id = True) -> None | dict:                            
        result = self.qlist(f"CALL table_info('{table_name}') RETURN *;", ignore_exception = True)
        if result is not None:
            return {info[1]: info[2] for info in result if not remove_id or info[1] != 'ID'}

    def delete_table(self, table_name):        
        return self.execute( f'DROP TABLE {table_name};')
            
    def get_table(self, id = None, limit = 100, headers = None, rows = None, fields = None):        
        if id:            
            if rows and fields is None:
                if not headers:
                    self.message_logger(f'headers are not defined!')
                    return None
                types = [None] * len(headers)                
                for row in rows:
                    for j, cell in enumerate(row):
                        if cell is not None:
                            ktype = kuzu_data_type(cell)
                            if ktype:
                                if types[j] is None:
                                    types[j] = ktype
                                elif types[j] != ktype:
                                    if types[j] in number_types and ktype in number_types:
                                        types[j] = "DOUBLE"
                                    else:
                                        self.message_logger(f'Conflict types for {id} table in {j} column: {types[j], ktype}!')
                                        return None                    
                if None in types:
                    index = types.index(None)
                    self.message_logger(f'Rows data doesnt contain allowed values for {headers[index]} column!')
                    return None
                fields = {headers[i]: type for i, type in enumerate(types)}
                        
            if (table_fields := self.get_table_fields(id)) is not None:
                if not equal_fields_dicts(table_fields, fields):                    
                    if self.delete_table(id):      
                        self.message_logger(f'Node table {id} was deleted because of fields contradiction!', 'warning')          
                else:
                    return self.tables.get(id) or Dbtable(id, self, limit, table_fields)
                        
        return self.create_table(id, fields, limit, rows)   

    def get_table_params(self, params):    
        return {k: v for k, v in params.items() if k in self.table_params}   

    def set_db_list(self, gui_table):
        table = self.get_table(**self.get_table_params(gui_table.__dict__))
        tlst = table.list
        gui_table.rows = tlst        
                    
    def create_table(self, id, fields : dict, limit = 100, rows = None):                
        specs = ','.join(f'{prop} {type}' for prop, type in fields.items())
        query = f"CREATE NODE TABLE {id}({specs},ID SERIAL, PRIMARY KEY(ID))"
        self.execute(query)
        table = Dbtable(id, self, limit, fields)
        if rows:
            table.list.extend(rows)
        return table
    
    def update_row(self, table_id, row_id, props, in_node = True):        
        set_props = dict_to_cypher_set(props)
        query = f'MATCH (a: {table_id}) WHERE a.ID = {row_id} {set_props}' if in_node else\
            f'MATCH ()-[a: {table_id}]->() WHERE a.ID = {row_id} {set_props}'
        return self.execute(query) 
        
    def qlist(self, query, func = None, ignore_exception = False):                
        if answer := self.execute(query, ignore_exception):            
            result = []
            while answer.has_next():
                value = answer.get_next()
                result.append(func(value) if func else value)
            return result
    
    def qiter(self, query, func = None, ignore_exception = False):        
        answer = self.execute(query, ignore_exception)
        while answer.has_next():
            value = answer.get_next()
            yield func(value) if func else value        
            
class Dbtable:
    def __init__(self, id, db, limit = 100, table_fields = None) -> None:        
        self.db = db
        db.tables[id] = self
        self.id = id              
        self.table_fields = table_fields
        self.limit = limit    
        self.node_columns = list(db.conn._get_node_property_names(id).keys())[:-1]            
        self.init_list()          

    @property
    def rel_table_names(self):
        return self.db.conn._get_rel_table_names() 
    
    def default_index_name2(self, link_table):
        return f'{self.id}2{link_table}'
    
    def calc_linked_rows(self,  index_name, link_ids, include_rels = False, search = ''):
        condition = f'b.ID in {link_ids}'
        rel_info = ', r.*' if include_rels else ''
        query = f"""
            MATCH (a:{self.id})-[r:{index_name}]->(b:User)
            WHERE {condition}
            RETURN a.*{rel_info}
            ORDER BY a.ID ASC
        """        
        lst = self.db.qlist(query)         
        return Dblist(self, cache = lst)
        
    def get_rel_fields2(self, tname, fields : dict = None, relname = None):
        """return name of link table and fields and its fields dict"""
        if not relname:
            relname = self.default_index_name2(tname)
        rel_table_fields = self.db.get_table_fields(relname)
        if isinstance(rel_table_fields, dict):
            if isinstance(fields, dict):
                if equal_fields_dicts(rel_table_fields, fields):
                    return relname, rel_table_fields
                else:
                    self.db.delete_table(relname)            
            else:
                fields = rel_table_fields
        elif fields is None:
            fields = {}

        if not any(info['name']  == relname for info in self.rel_table_names):                        
            fprops = ''.join(f', {field} {type}' for field, type in fields.items()) if fields else ''
            fprops += ', ID SERIAL'
            query = f"CREATE REL TABLE {relname}(FROM {self.id} TO {tname} {fprops})"
            self.db.execute(query)
            self.rel_table_names.append({'name' : relname})
        return relname, fields
    
    def add_link(self, snode_id, link_table, tnode_id, link_fields = None, link_index_name = None):
        """return added link"""
        if link_index_name is None:
            link_index_name = self.default_index_name2(link_table)
        if link_fields is None:
            link_fields = {}
        query = f"""
            MATCH (a:{self.id}), (b:{link_table})
            WHERE a.ID = {snode_id} AND b.ID = {tnode_id}
            CREATE (a)-[r:{link_index_name} {{{Properties(link_fields)}}}]->(b)            
            RETURN r.*
            """
        lst = self.db.qlist(query)
        return lst[0]
    
    def add_links(self, link_table, snode_ids : iter, tnode_id, link_index_name = None):
        result = []
        for id in snode_ids:
            result.append(self.add_link(id, link_table, tnode_id, link_index_name = link_index_name))
        return result
    
    def delete_link(self, link_table_id, link_id, index_name = None):
        if not index_name:
            index_name = self.default_index_name2(link_table_id)
        query = f"""
        MATCH (:{self.id})-[r:{index_name}]->(:{link_table_id})
        WHERE r.ID = {link_id}
        DELETE r
        """
        self.db.execute(query)

    def delete_links(self, link_table_id, link_node_id = None, source_ids = None, link_ids = None, index_name = None):
        if not index_name:
            index_name = self.default_index_name2(link_table_id)
        
        if link_ids:
            condition = f'r.ID in {link_ids}'            
        else:
            if not isinstance(source_ids, list):
                source_ids = list(source_ids)                        
            condition = f'(a.ID in {source_ids}) AND b.ID = {link_node_id}'
        query = f"""
        MATCH (a:{self.id})-[r:{index_name}]->(b:{link_table_id})
        WHERE {condition}
        DELETE r
        """
        self.db.execute(query)

    def init_list(self):                
        list = self.read_rows(limit = self.limit)
        length = len(list)
        #possibly the table has more rows        
        if length == self.limit:            
            ql = self.db.qlist(f"MATCH (n:{self.id}) RETURN count(n)")
            self.length = ql[0][0]
        else:
            self.length = length
        self.list = Dblist(self, list)
        
    def read_rows(self, skip = 0, limit = 0):
        query = qb().match().node(self.id, 'a').return_literal('a.*').order_by('a.ID')
        if skip:
            query = query.skip(skip)
        query = query.limit(limit if limit else self.limit)
        return self.db.qlist(query)    

    def assign_row(self, row_array):
        return self.db.update_row(self.id, row_array[-1], 
            {name : value for name, value in zip(self.node_columns, row_array)})

    def delete_row(self, id):
        query = query_offset(self.id, id)
        self.length -= 1
        return self.db.execute(query.detach_delete('a'))
    
    def delete_rows(self, ids):
        condition = f'a.ID in {ids}'
        query = f"""
        MATCH (a:{self.id})
        WHERE {condition}
        DELETE a
        """
        return self.db.execute(query)
    
    def clear(self, detach = False):
        query = f'MATCH (a:{self.id})'
        if detach:
            query += ' DETACH DELETE a'
        else:
            query += ' DELETE a'
        self.length = 0
        return self.db.execute(query)
    
    def append_row(self, row):
        """row can be list or dict, returns new row"""        
        if isinstance(row, list):
            props = {name: value for name, value in zip(self.node_columns, row) if value is not None}
                      
        answer = self.db.execute(qb().create().node(self.id, 'a', props).return_literal('a.*'))        
        if answer and answer.has_next():                        
            self.length += 1
            return answer.get_next()        
    
    def append_rows(self, rows):
        """row can be list or dict"""
        rows_arr = []
        for row in rows:            
            row = {name: value for name, value in zip(self.node_columns, row)} if not isinstance(row, dict) else row
            srow = f' {{{Properties(row).to_str()}}}'
            rows_arr.append(srow)      
        rows_arr = ','.join(rows_arr)      
        
        query = (qb().with_(f'[{rows_arr}] AS rows')
            .unwind('rows AS row')
            .create()
            .node(self.id, 'n', {p: f'row.{p}' for p in self.node_columns}, escape=False)
            .return_literal('n.*'))
        
        self.length += len(rows)        
        return self.db.qlist(query)  

