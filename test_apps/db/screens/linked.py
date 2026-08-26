from unisi import *
from data import *
name = 'Linked tables'
order = 1

utable = Table("Users", id = 'User', limit = 150, ids = True,
    rows= users, headers=['name', 'age', 'height'])
otable = Table("Orders", id = 'Orders', limit = 150, ids = True, 
    rows= orders, headers=['name', 'sum'], link = (utable, {'type' : 'string', 'weight' : 'double'}))

# `link=(utable, {...})` above only creates the junction TABLE that will
# hold the relationship between Users and Orders (see
# docs/persistent_tables.md, §5 "Many-to-Many"). It does not create any
# link ROWS: the 20000 Users and 1000 Orders inserted via `rows=` above are
# still completely independent records until something actually calls
# add_link/add_links, so selecting a User shows zero linked Orders. The
# block below seeds a demo set of links so the screen has something to
# show. For the general recipe (and why the idempotency guard below is
# needed), see docs/persistent_tables.md, "Seeding linked data at startup".
dbt = otable.rows.dbtable
rel_name = otable.rows.link[2]  # junction table name, e.g. 'Orders2User'

if not Unishare.db.qlist(f'SELECT 1 FROM [{rel_name}] LIMIT 1'):
    # otable.rows is already the (currently empty) filtered selection view
    # at this point -- see §5.4 -- so read the raw rows straight from
    # SQLite instead of going through otable.rows.
    all_orders = dbt.read_rows(limit=dbt.length)
    link_types = ['purchase', 'subscription', 'gift']
    orders_per_user = 5
    linked_users = 200  # first 200 of the 20000 Users get Orders

    for ui in range(linked_users):
        user_id = utable.rows[ui][-1]
        for k in range(orders_per_user):
            order_id = all_orders[(ui * orders_per_user + k) % len(all_orders)][-1]
            dbt.add_link(order_id, utable.id, user_id, link_index_name=rel_name,
                link_fields={'type': link_types[k % len(link_types)], 'weight': round(1 / (k + 1), 2)})

    # A handful of Orders shared between two Users (e.g. a split purchase),
    # to also demonstrate the many-to-many - not just many-to-one - side of
    # the relationship: an Order can belong to more than one User.
    for k in range(10):
        order_id = all_orders[k][-1]
        user_id = utable.rows[linked_users + k][-1]
        dbt.add_link(order_id, utable.id, user_id, link_index_name=rel_name,
            link_fields={'type': 'shared', 'weight': 0.5})

blocks = [Block('TBlock', [], [utable, otable])]