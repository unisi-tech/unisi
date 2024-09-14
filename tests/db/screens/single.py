from unisi import *
name = 'Single table'
order = 0

ctable = Table("Companies", id = 'Company', fields ={'name' : 'string','place': 'string','phone': 'string'})

def change_ids(button, value):
    ctable.ids = value
    ctable.calc_headers()
    return ctable

def add_data(*_):
    rows_len = len(ctable.rows)
    data = [[f'Company {i}', f' Place {i}', f'Phone {i}']  for i in range(rows_len, rows_len + 1000)]
    ctable.rows.extend(data)    
    #does not need to return persistent table!

def clear_data(*_):
    ctable.rows.clear()
    #does not need to return persistent table!

blocks = [Block('TBlock', [Switch('Show ID', False, change_ids), Button('+ data',add_data), Button('Clear data', clear_data)], ctable)]