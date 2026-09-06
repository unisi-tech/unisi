# Copyright © 2024 UNISI Tech. All rights reserved.
from .units import Unit, ChangedProxy
from .common import *
from .dbunits import Dblist, dbupdates
from .llmrag import get_property
import asyncio, dataclasses
from collections import OrderedDict

relation_mark = 'Ⓡ'
exclude_mark = '✘'
max_len_rows4llm = 30

def _dataclass_instance(row):
    """If `row` is a dataclass instance -- optionally wrapped in a
    ChangedProxy, see _row_field_names -- return the real, unwrapped
    instance, so callers needing more than positional access (its actual
    type, for _blank_dataclass_row/_infer_row_type) can get at it.
    Otherwise None -- including for a plain dict, unlike the broader
    _row_field_names (a dict has no *type* to speak of here, only field
    names).
    """
    obj = row._obj if isinstance(row, ChangedProxy) else row
    return obj if dataclasses.is_dataclass(obj) and not isinstance(obj, type) else None

def _row_field_names(row):
    """Positional field names for a *named* row -- a dataclass instance,
    or a plain dict -- optionally wrapped in a ChangedProxy (units.py: a
    table's rows get wrapped once the table is claimed by a user, see
    Unit.set_reactivity). None for an ordinary list/tuple row, which needs
    no name translation at all.

    A dict counts as "named" for exactly one reason: a dataclass row
    degrades to a plain per-field dict through a persist.py save/restore
    round trip (persist.py only ever reconstructs *Unit* instances,
    matched by id -- an arbitrary dataclass instance isn't a Unit, has no
    id, and comes back however its __getstate__/__dict__ happened to
    look). Table._after_persist_restore below turns such a dict back into
    a real `row_type` instance when it can, but a table with no row_type
    to reconstruct against is left holding these dicts indefinitely, and
    they must stay *safely* editable -- by field name, not by silently
    writing to a bogus integer key a plain dict would otherwise accept.

    A dict's own key order stands in for a dataclass's dataclasses.fields()
    declaration order -- both preserve insertion order in practice (a
    dataclass's __dict__/__getstate__ does, same as any dict; Python dicts
    always do) -- so a row is addressed positionally the same way either
    way, without this function's caller ever needing to know it used to be
    a dataclass.
    """
    obj = row._obj if isinstance(row, ChangedProxy) else row
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return [f.name for f in dataclasses.fields(obj)]
    if isinstance(obj, dict):
        return list(obj.keys())
    return None

def row_values(row):
    """`row`'s cell values, positionally aligned with `headers`: `row`
    itself when it's already a plain sequence (the documented case --
    README, docs/unisi-programming-spec.md §12: a row is a list of cell
    values), or its values in _row_field_names() order when `row` is a
    "named" row (a dataclass instance, or a dict -- see
    _row_field_names). That order is on the caller to keep in sync with
    `headers`, exactly like a list row's cell order already has to be.

    Reads through `row` itself -- row[name] / getattr(row, name), not
    through an unwrapped copy -- so a ChangedProxy-wrapped row keeps
    wrapping non-atomic field values the same way it already wraps a plain
    list's items (see ChangedProxy.__iter__ in units.py).
    """
    names = _row_field_names(row)
    if names is None:
        return row
    obj = row._obj if isinstance(row, ChangedProxy) else row
    if isinstance(obj, dict):
        return [row[name] for name in names]
    return [getattr(row, name) for name in names]

def set_cell(row, index, value):
    """Write `value` into `row`'s cell at position `index` (0-based,
    aligned with `headers`): `row[index] = value` when `row` is a plain
    mutable sequence, `row[name] = value` when `row` is a dict, or setattr
    onto the matching field when `row` is a dataclass instance -- see
    _row_field_names.

    A cell edit (accept_cell_value below, and emit()'s LLM autofill)
    addresses a cell by *position* -- the wire protocol's edit event
    carries a column index, since the client has no way to know a Python
    dataclass' attribute names (or, for a dict row, which key means what)
    -- so `index` is translated to the matching name before the value is
    written. A plain dataclass instance supports neither __getitem__ nor
    __setitem__, so `row[index] = value` would otherwise raise TypeError;
    a plain dict *does* accept `row[index] = value` without complaint, but
    silently as a bogus new integer key instead of updating the field it
    was meant to -- the exact silent-corruption failure mode this
    function exists to prevent.

    Writing through `row` itself (rather than through an unwrapped copy)
    keeps this transparent to ChangedProxy: its __setattr__/__setitem__
    already forward the write to the wrapped object and mark the table
    changed, exactly like they do today for a list row.
    """
    names = _row_field_names(row)
    if names is None:
        row[index] = value
        return
    obj = row._obj if isinstance(row, ChangedProxy) else row
    name = names[index]
    if isinstance(obj, dict):
        row[name] = value
    else:
        setattr(row, name, value)

def _blank_dataclass_row(cls):
    """A new `cls` instance with every field set to None -- the same
    "blank, to be filled in one cell at a time" placeholder a plain-list
    table already gets from append_table_row's `[None] * len(headers)`.

    Built by bypassing __init__ (object.__new__ + object.__setattr__ per
    field) rather than calling `cls()`, so this works regardless of
    whether `cls` has required fields with no default, validates its
    arguments in __post_init__, or is frozen -- a frozen dataclass
    overrides __setattr__ to raise FrozenInstanceError for any ordinary
    attribute assignment, even from outside __init__; object.__setattr__
    is the same escape hatch dataclasses' own generated __init__ uses
    internally to set attributes on a frozen instance.
    """
    instance = object.__new__(cls)
    for f in dataclasses.fields(cls):
        object.__setattr__(instance, f.name, None)
    return instance

def _dataclass_from_dict(cls, saved):
    """Reconstruct a `cls` instance from a plain dict of its field values
    -- the shape a dataclass row degrades to through a persist.py
    save/restore round trip (see Table._after_persist_restore). Same
    __init__-bypassing construction as _blank_dataclass_row, so a frozen
    `cls` works and old saved data isn't re-validated through
    __post_init__.

    Tolerant of schema drift between when a row was saved and now: a field
    `cls` has that the saved dict doesn't (a field added since) reads back
    as None, same as a freshly-appended blank row; a key the saved dict
    has that `cls` no longer declares (a field removed since) is simply
    never looked at, rather than raising.
    """
    instance = object.__new__(cls)
    for f in dataclasses.fields(cls):
        object.__setattr__(instance, f.name, saved.get(f.name))
    return instance

def _infer_row_type(rows):
    """The dataclass type of `rows`'s first dataclass-instance row, if
    any -- Table's automatic `row_type` when the caller didn't pass one
    explicitly (see Table.__init__). None for an empty/list-only `rows`,
    or when `rows` itself hasn't been given at all yet.
    """
    if not rows:
        return None
    dc = _dataclass_instance(rows[0])
    return type(dc) if dc is not None else None

def _blank_row_like(rows, header_count):
    """The default new row for append_table_row's non-persistent branch:
    every cell None, shaped to match whatever the table's *existing* rows
    already are --
      - a fresh instance of the same dataclass when `rows` holds dataclass
        rows, so the appended row stays gettable/settable exactly like the
        rest of the table;
      - a dict with the same keys (values None) when `rows` holds dict
        rows (see _row_field_names -- most likely a table whose dataclass
        rows degraded through a persist.py restore with no row_type to
        reconstruct against, see Table._after_persist_restore);
      - the classic `[None, ...]` list otherwise, including when `rows` is
        still empty and there's nothing to match.
    """
    if rows:
        first = rows[0]
        if (dc := _dataclass_instance(first)) is not None:
            return _blank_dataclass_row(type(dc))
        obj = first._obj if isinstance(first, ChangedProxy) else first
        if isinstance(obj, dict):
            return {key: None for key in obj.keys()}
    return [None] * header_count

def get_chunk(obj, start_index):
    if not isinstance(start_index, int) or isinstance(start_index, bool):
        return Error(f"get requires an integer row index, got {start_index!r}", obj)
    delta, data = obj.rows.get_delta_chunk(start_index)
    return {'update': 'updates', 'index': delta, 'data': data}

def accept_cell_value(table, dval: dict):            
    value = dval['value']
    if not isinstance(value, bool):
        try:
            value = float(value)        
        except:
            pass            
    if hasattr(table,'id'):
        dval['value'] = value
        if update := table.rows.update_cell(**dval):
            update['exclude'] = True       
    else:        
        set_cell(table.rows[dval['delta']], dval['cell'], value)
            
def delete_table_row(table, value):    
    if value is not None and value != []:
        if hasattr(table, 'link') and table.filter:
            link_table, rel_props, rel_name = table.rows.link
            if not isinstance(value, list):                                
                value = [value]
            if rel_name is None:
                # many-to-one: clear link_id on the row
                for index in value:
                    table.rows.dbtable.clear_fk(table.rows[index][-1])
                table.__link_table_selection_changed__(link_table, link_table.value)
            else:
                # many-to-many: delete junction rows
                link_ids = [table.rows[index][-1] for index in value]
                table.rows.dbtable.delete_links(link_table.id, link_ids=link_ids, index_name=rel_name)
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

def append_table_row(table, search_str = ''):
    ''' append has to return new row, value is the search string value in the table'''    
    if getattr(table,'id', None):          
        new_row = [None] * len(table.headers)           
        new_row = table.rows.append(new_row)        
        if hasattr(table, 'link') and table.filter:
            link_table, _, rel_name = table.rows.link
            for linked_idx in link_table.selected_list:
                master_id = link_table.rows[linked_idx][-1]
                if rel_name is None:
                    # many-to-one: stamp link_id on the new row
                    table.rows.dbtable.set_fk(new_row[-1], master_id)
                    new_row[table.rows.dbtable.node_columns.index(table.rows.dbtable.LINK_ID)] = master_id
                else:
                    # many-to-many: insert junction row
                    relation = table.rows.dbtable.add_link(
                        new_row[-1], link_table.id, master_id, link_index_name=rel_name)
                    if relation:
                        new_row.extend(relation)
                break      
    else:           
        new_row = _blank_row_like(table.rows, len(table.headers))
        table.rows.append(new_row)
    return new_row

class Table(Unit):
    def __init__(self, *args, panda = None, row_type = None, **kwargs):
        if panda is not None:
            self._mark_changed = None
            self.mutate(PandaTable(*args, panda=panda, **kwargs))
        else:
            super().__init__(*args, **kwargs)    
            set_defaults(self, dict(headers = [], type = 'table', value = None, rows = [], 
                            editing = False, dense = True, max_column_length = 40))
            self.__headers__ = self.headers[:]
            # Not client-facing (leading underscore -> Unit.__getstate__
            # skips it when building the JSON state, and Unit.__setattr__
            # skips the ChangedProxy-wrapping/_mark_changed dance for it
            # too -- appropriately, since it's a static schema declaration
            # the developer's own code provides, not reactive UI state).
            # See _after_persist_restore for what it's actually for.
            object.__setattr__(self, '_row_type', row_type or _infer_row_type(self.rows))
        if hasattr(self,'id'):             
            if Unishare.db:
                Unishare.db.set_db_list(self)
            else:
                raise AssertionError('Config db_path is not defined!')            
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
                if not hasattr(link_table, 'id'):
                    raise AttributeError('Linked table has to be persistent!')

                # link = utable          → many-to-one (FK `link_id` column)
                # link = [utable, {...}] → many-to-many (junction table) —
                #   even when the payload dict is empty: `link = [utable, {}]`
                #   is still an explicit *list*, i.e. "many-to-many with no
                #   extra fields", and must not collapse into many-to-one
                #   just because `not {}` happens to be True. Route on the
                #   *shape* of self.link itself (was a list/tuple given at
                #   all?), not on whether prop_types happens to be empty —
                #   otherwise link=[utable, {}] is indistinguishable from
                #   link=utable and silently gets an FK column instead of a
                #   junction table.
                many_to_one = not isinstance(self.link, (list, tuple))

                if many_to_one:
                    # ── Many-to-one: FK column link_id in this table ──────
                    self.rows.dbtable.setup_fk(link_table.id)
                    rel_fields = {}
                    self.rows.dbtable.link_info = link_table, [], None
                else:
                    # ── Many-to-many: junction table ──────────────────────
                    rel_name, rel_fields = self.rows.dbtable.setup_junction(
                        link_table.id, prop_types, rel_name or None)
                    self.rows.dbtable.link_info = link_table, list(prop_types.keys()), rel_name

                self.rows.link = self.rows.dbtable.link_info
                self.link = rel_fields
                
                @Unishare.handle(link_table,'changed')
                def link_table_selection_changed(master_table, val, init = False):
                    lstvalue = val if isinstance(val, list) else [val] if val != None else []
                    if lstvalue:
                        link_ids = [link_table.rows[v][-1] for v in lstvalue]
                        if many_to_one:
                            link_rows = self.rows.dbtable.calc_linked_rows_fk(link_ids, self.search)
                        else:
                            link_rows = self.rows.dbtable.calc_linked_rows(rel_name, link_ids, link_table.id, self.filter, self.search)
                    else:
                        link_rows = Dblist(self.rows.dbtable, cache = [])
                    if self.filter:                    
                        self.clean_selection()
                        link_rows.link = self.rows.dbtable.link_info
                        self.rows = link_rows
                    else: 
                        # link_rows[i][-1] is each linked row's DB *id*
                        # (calc_linked_rows/calc_linked_rows_fk both append
                        # it last) - but self.value indexes by *position* in
                        # the unfiltered list assigned below (matching
                        # Dblist/iiid elsewhere), and id == position only for
                        # a table that's never had a row deleted. Translate
                        # through index_of_id rather than assuming the two
                        # coincide.
                        dbtable = self.rows.dbtable
                        self.value = [dbtable.index_of_id(link_rows[i][-1]) for i in range(len(link_rows))]
                        if self.search:
                            self.rows = self.rows.dbtable.search_rows(self.search)
                        elif self.rows.cache is not None:
                            self.rows = self.rows.dbtable.list
                    if not init:
                        master_table.accept(val)              
                        return self     
                link_table_selection_changed(link_table, link_table.value, True)
                self.__link_table_selection_changed__ = link_table_selection_changed

                @Unishare.handle(self,'filter')
                def filter_status_changed(table, value):
                    table.filter = value
                    link_table_selection_changed(link_table, link_table.value, True)
                    table.calc_headers()
                    return table                
                
                @Unishare.handle(self,'changed')
                def changed_selection_causes__changing_links(self, new_value):                   
                    if link_table.value is not None and link_table.value != []:
                        if not self.filter and not isinstance(link_table.value, list | tuple):
                            if self.editing:
                                if many_to_one:
                                    # set/clear link_id on the newly selected row
                                    master_id = link_table.rows[link_table.value][-1]
                                    actual = set(new_value if isinstance(new_value, list) else [] if new_value is None else [new_value])
                                    old = set(self.value if isinstance(self.value, list) else ([] if self.value is None else [self.value]))
                                    for idx in actual - old:
                                        self.rows.dbtable.set_fk(self.rows[idx][-1], master_id)
                                    for idx in old - actual:
                                        self.rows.dbtable.clear_fk(self.rows[idx][-1])
                                else:
                                    actual = set(new_value if isinstance(new_value, list) else [] if new_value is None else [new_value])
                                    old = set(self.value if isinstance(self.value, list) else ([] if self.value is None else [self.value]))                        
                                    master_id = link_table.rows[link_table.value][-1]
                                    deselected = old - actual                        
                                    if deselected:
                                        deselected_ids = [self.rows[idx][-1] for idx in deselected]
                                        self.rows.dbtable.delete_links(link_table.id, master_id, deselected_ids, index_name=rel_name)
                                    selected = actual - old
                                    if selected:
                                        selected_ids = [self.rows[idx][-1] for idx in selected]
                                        self.rows.dbtable.add_links(link_table.id, selected_ids, master_id, link_index_name=rel_name)
                            else:
                                return Warning('The linked table is not in edit mode', self)
                    return self.accept(new_value)    
            @Unishare.handle(self,'search')
            def search_changed(table, value):
                table.search = value
                if has_link:
                    link_table_selection_changed(link_table, link_table.value, True)
                else:
                    dbtable = table.rows.dbtable
                    if value:
                        table.rows = dbtable.search_rows(value)
                    else:
                        dbtable.init_list()
                        table.rows = dbtable.list
                table.clean_selection()
                return table
            self.calc_headers()                                
                    
        elif hasattr(self,'ids'):
            raise ValueError("Only persistent tables can have 'ids' option!")

        if getattr(self,'edit', True): 
            set_defaults(self,{'delete': delete_table_row, 'append': append_table_row, 'modify': accept_cell_value})   

    def _after_persist_restore(self):
        """Called once, generically, right after persist.py finishes
        applying a saved dict onto this table (see persist.py's
        _smart_apply_dict, which looks for this exact method name on any
        restored unit -- it has no idea what a Table or a dataclass row
        is; this is purely tables.py's own follow-up).

        A dataclass row degrades to a plain per-field dict through the
        save/restore round trip (see _row_field_names' docstring):
        persist.py's _rebuild_value only ever reconstructs *Unit*
        instances, matched by id -- an arbitrary dataclass instance isn't
        a Unit and has no id, so it comes back however its
        __getstate__/__dict__ happened to look, which is indistinguishable
        from an ordinary dict once restored. If this table knows what
        dataclass its rows should be (self._row_type -- explicit via
        Table(..., row_type=...), or inferred from an example row at
        construction, see __init__), each restored dict is turned back
        into a real instance of it here, so the table looks exactly like
        it did before the restart: table.rows[i].some_field, not
        table.rows[i]['some_field'].

        No row_type to reconstruct against (never given, and rows started
        out empty so there was nothing to infer from either)? Left as
        plain dicts -- safely editable by field name either way, see
        _row_field_names/set_cell -- rather than guessing at a class.

        Uses object.__setattr__, like persist.py's own _smart_apply_dict,
        rather than a normal `self.rows = ...`: nothing about the row
        *values* actually changed from the client's point of view (a
        dataclass instance and the dict it degraded from serialize to the
        exact same JSON shape either way, see docs/protocol.md), so this
        shouldn't mark the table changed or queue a redundant client
        update on top of whatever the restore itself already triggers.
        """
        row_type = self._row_type
        if row_type is None or not self.rows:
            return
        object.__setattr__(self, 'rows', [
            _dataclass_from_dict(row_type, row) if isinstance(row, dict) else row
            for row in self.rows
        ])

    @property
    def compact_view(self) -> str:
        """only selected are sended to llm"""
        selected = self.selected_list        
        if not selected and len(self.rows) < max_len_rows4llm:
            selected = range(len(self.rows))        
        str_rows = ';'.join(','.join(f'{field}: {value}' for field, value in zip(self.headers, row_values(self.rows[index]))) for index in selected)
        return f'{self.name} : {str_rows}' 
    
    @property
    def selected_list(self):                            
        return [] if self.value is None else self.value if isinstance(self.value, list) else [self.value]   

    def clean_selection(self):        
        self.value = [] if isinstance(self.value,tuple | list) else None
        return self    
    
    @property
    def panda(self):
        if gp := getattr(self,'__panda__',None):
            return gp() 
    
    def calc_headers(self):        
        """only for persistent"""
        table_fields = self.rows.dbtable.table_fields
        self.headers = self.__headers__[:] if self.__headers__ else [pretty4(prop)for prop in table_fields]
        only_node_headers = len(self.headers) == len(table_fields)
        if self.ids:
            self.headers.insert(len(table_fields), 'ID')
        elif self.filter:
            self.headers.insert(len(table_fields), exclude_mark + 'ID')
        if self.filter and hasattr(self, 'link'):
            if only_node_headers:
                self.headers.extend([relation_mark + pretty4(link_field) for link_field in self.link])
            if self.ids:
                self.headers.append(relation_mark + 'ID')
    
    async def emit(self, *_):        
        """calcute llm field values for selected rows if they are None"""        
        if Unishare.llm_model and getattr(self, 'llm', None) is not None:              
            tasks = []
            for index in self.selected_list:
                values = {field: value for field, value in zip(self.headers, row_values(self.rows[index])) if value}
                for fld, deps in self._llm_dependencies.items():                    
                    if fld not in values:                        
                        if deps is True:
                            context = values
                        else:
                            context = OrderedDict()
                            for dep in deps:
                                if isinstance(dep, str):
                                    value = values.get(dep, None)
                                elif isinstance(dep, Unit):
                                    value = dep.value
                                else:
                                    raise AttributeError(f'Invalid llm parameter {dep} in {self.name} element!')
                                if value is None:
                                    if self.llm: #exact
                                        continue   #not all fields
                                else:
                                    context[dep if isinstance(dep, str) else dep.name] = value
                        if context:                                                    
                            async def assign(index, fld, context):
                                set_cell(self.rows[index], self.headers.index(fld), await get_property(fld, context))
                            context =  ','.join(f'{fld}: {val}' for fld, val in context.items())
                            tasks.append(asyncio.create_task(assign(index, fld, context)))
            if tasks:
                await asyncio.gather(*tasks)
                return self
    @property    
    def is_base_table_list(self):
        """is table in basic view mode"""
        if hasattr(self, 'id'):
            dbtable = self.rows.dbtable
            return dbtable.list is self.rows
        
def delete_panda_row(table, value):    
    pt = table.panda
    def delete_in_panda(row_index):
        if row_index < 0 or row_index >= len(pt):
            raise ValueError("Row number is out of range")
        pt.drop(index = row_index,  inplace=True)

    if isinstance(value, list | tuple):                    
        value.sort(reverse=True)
        for row_index in value:            
            delete_in_panda(row_index)        
    else:            
        delete_in_panda(value)        
    
    pt.reset_index(drop=True, inplace=True)
    delete_table_row(table, value)    

def accept_panda_cell(table, value_pos: dict):
    value = value_pos['value']
    if not isinstance(value, bool):
        try:
            value = float(value)        
        except:
            pass                
    row_index, col_index = value_pos['delta'], value_pos['cell']
    table.panda.iat[row_index,col_index] = value
    accept_cell_value(table, value_pos)

def append_panda_row(table, row_index):    
    df = table.panda
    new_row = append_table_row(table, row_index)
    df.loc[len(df)] = new_row 
    return new_row    

class PandaTable(Table):
    """ panda = opened panda table"""
    def __init__(self, name, *args, panda = None, fix_headers = True, **kwargs):
        Unit.__init__(self, name, *args, **kwargs)                  
        set_defaults(self, dict(type = 'table', value = None, editing = False, dense = True, max_column_length = 40))        
        if panda is None:
            raise Exception('PandaTable has to get panda = pandaTable as an argument.')
        self.headers = panda.columns.tolist()
        if fix_headers:
            self.headers = [pretty4(header) for header in self.headers]        
        self.rows = panda.values.tolist()
        self.__panda__ = lambda: panda

        if getattr(self,'edit', True): 
            set_defaults(self,{'delete': delete_panda_row, 'append': append_panda_row,
                'modify': accept_panda_cell})
    
    