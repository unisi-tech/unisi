from unisi import *
name = 'Single table'
order = 0

ctable = Table("Companies", id = 'Company', fields ={'name' : 'STRING','place': 'STRING','phone': 'STRING'})

def change_ids(button, value):
    ctable.ids = value
    ctable.calc_headers()
    return ctable

def add_data(button, _):
    rows_len = len(ctable.rows)
    data = [[f'Company {i}', f' Place {i}', f'Phone {i}']  for i in range(rows_len, rows_len + 1000)]
    ctable.rows.extend(data)
    return ctable


blocks = [Block('TBlock', [Switch('Show ID', False, change_ids), Button('+ data',add_data)], ctable)]