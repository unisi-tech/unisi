#!/usr/bin/env python3
"""
Демонстрация: два разных синтаксиса `link` у персистентных таблиц UNISI.

    link = other_table            -> many-to-one   (FK-колонка link_id)
    link = [other_table, {...}]   -> many-to-many  (junction-таблица),
                                      причём это верно и при {} -
                                      пустом словаре доп. полей.

Как запустить:
    1. Поместите файл в корень вашего чекаута unisi (рядом с папкой unisi/),
       либо поправьте UNISI_ROOT ниже на путь до него.
    2. python demo_link_m2o_vs_m2m.py

Часть 2 (M2M с пустым payload) требует применённого фикса из
many_to_one_vs_many_to_many_fix.patch - без него её assert'ы упадут,
потому что products получит колонку link_id вместо junction-таблицы.
"""
import sys
from pathlib import Path

UNISI_ROOT = Path(__file__).resolve().parent  # поправьте при необходимости
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))

from unisi.common import Unishare
from unisi.users import User
from unisi.db import Database
from unisi.tables import Table


class FakeUser:
    """Минимальная замена User (как в tests/units/conftest.py) - Table.__init__
    для персистентных таблиц ожидает user.handlers для регистрации 'changed'/'filter'."""
    def __init__(self):
        self.handlers = {}
    def register_changed_unit(self, unit, property=None, value=None):
        pass


def existing_sqlite_tables():
    cur = Unishare.db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cur.fetchall()}


def section(title):
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


User.last_user = FakeUser()
Unishare.db = Database(":memory:", message_logger=print)

# ────────────────────────────────────────────────────────────────
# Часть 1: link = other_table  ->  many-to-one
# ────────────────────────────────────────────────────────────────
section("1) link = customers  (без списка)  ->  MANY-TO-ONE")

customers = Table('Customers', id='customers',
                   fields={'company': str, 'country': str})

orders = Table('Orders', id='orders',
               fields={'product': str, 'amount': float, 'status': str},
               link=customers)               # <-- голая ссылка на таблицу

link_table, rel_fields, rel_name = orders.rows.link
print(f"link_table = {link_table.name}, rel_name = {rel_name!r}  (None = FK, не junction)")
assert rel_name is None

cols = Unishare.db.get_table_fields('orders')
print("Колонки orders:", cols)
assert 'link_id' in cols
assert 'orders2customers' not in existing_sqlite_tables()

dbt = orders.rows.dbtable
c1 = customers.rows.dbtable.append_row({'company': 'Acme', 'country': 'TH'})
c2 = customers.rows.dbtable.append_row({'company': 'Globex', 'country': 'US'})
o1 = dbt.append_row({'product': 'Laptop', 'amount': 1200.0, 'status': 'new'})
o2 = dbt.append_row({'product': 'Mouse', 'amount': 20.0, 'status': 'new'})
o3 = dbt.append_row({'product': 'Monitor', 'amount': 300.0, 'status': 'new'})

dbt.set_fk(o1[-1], c1[-1])   # Laptop  -> Acme
dbt.set_fk(o2[-1], c1[-1])   # Mouse   -> Acme
dbt.set_fk(o3[-1], c2[-1])   # Monitor -> Globex

acme_orders = dbt.calc_linked_rows_fk([c1[-1]])
print(f"Заказы Acme: {list(acme_orders)}")
assert len(acme_orders) == 2

dbt.clear_fk(o1[-1])
assert len(dbt.calc_linked_rows_fk([c1[-1]])) == 1
print("clear_fk(Laptop) -> у Acme остался 1 заказ. OK")

# тот же путь, которым идёт реальный UI при выборе клиента (Unishare.handle('changed'))
sel_index = customers.rows.dbtable.index_of_id(c2[-1])
orders.__link_table_selection_changed__(customers, sel_index)
print("После выбора Globex в UI: orders.rows =", list(orders.rows))
assert [row[0] for row in orders.rows] == ['Monitor']

print("\n✓ link = customers  ->  many-to-one работает")

# ────────────────────────────────────────────────────────────────
# Часть 2: link = [other_table, {}]  ->  many-to-many, без payload-полей
# ────────────────────────────────────────────────────────────────
section("2) link = [tags, {}]  (список с пустым словарём)  ->  MANY-TO-MANY")

tags = Table('Tags', id='tags', fields={'tag': str})

products = Table('Products', id='products',
                  fields={'name': str, 'price': float},
                  link=[tags, {}])          # <-- список, пусть даже без доп. полей

link_table2, rel_fields2, rel_name2 = products.rows.link
print(f"link_table = {link_table2.name}, rel_name = {rel_name2!r}  (строка = junction-таблица)")
assert rel_name2 is not None

cols2 = Unishare.db.get_table_fields('products')
print("Колонки products:", cols2)
assert 'link_id' not in cols2
assert rel_name2 in existing_sqlite_tables()

print(f"\n✓ link = [tags, {{}}]  ->  many-to-many (junction '{rel_name2}'), без link_id")

section("Итог")
print("Оба варианта работают корректно и не путаются между собой:")
print("  link = table_x           ->  many-to-one   (FK link_id)")
print("  link = [table_x, {...}]  ->  many-to-many  (junction-таблица)")
