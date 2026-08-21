# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/tables.py: the module-level row handlers (get_chunk,
accept_cell_value, delete_table_row, append_table_row, delete_panda_row,
accept_panda_cell, append_panda_row) and the Table / PandaTable classes,
covering plain in-memory tables, pandas-backed tables, and persistent
(DB-backed) tables including linked (many-to-one and many-to-many) ones.

Persistent-table tests use the `memdb` fixture (see conftest.py): a fresh
in-memory Database wired in as Unishare.db, plus a FakeUser wired in as
User.last_user so Unishare.handle(...) (used for the unconditional `search`
handler, and for `changed`/`filter` on linked tables) has somewhere real to
register into.

A note on writing cell values in persistent-table tests: `table.rows[i][j]
= value` looks like it should work (and silently "succeeds" -- no
exception), but it only mutates a transient, disconnected snapshot object
handed back by Dblist.__getitem__ -- it never reaches the database. Real
cell edits go through accept_cell_value → Dblist.update_cell(), which is
also what the framework's own `modify` handler calls. All persistent-table
tests below set cell values via `table.modify(table, {...})` for exactly
this reason -- using raw indexing would silently produce tests that pass
today and quietly stop meaning anything the moment a row gets re-fetched
from the DB (e.g. after any filter/search/delete refresh).

Regression tests for the bugs fixed alongside this suite are labelled
`test_regression_*`:

  * delete_table_row's guard checked `table.selected_list` (the table's own
    .value) instead of the `value` argument it was actually asked to
    delete -- calling the handler with an explicit value while .value
    didn't happen to match (e.g. a per-row delete button with no separate
    selection step) silently deleted nothing.
  * delete_panda_row left `pt.reset_index(...)` commented out, so deleting
    a row desynced the DataFrame's labels from its positions; the next
    append_panda_row (`df.loc[len(df)] = new_row`) could then silently
    overwrite an existing row instead of adding a new one -- real data
    loss, not just a cosmetic glitch.
  * changed_selection_causes__changing_links' many-to-many branch passed
    raw row-selection *indices* to add_links/delete_links where the DB
    layer expects actual row ids (it builds SQL directly from them) --
    selecting a child row to link it to the current master crashed with a
    FOREIGN KEY constraint failure, and deselecting one silently deleted
    nothing.
"""
import pandas as pd
import pytest

from unisi.common import Unishare
from unisi.units import Unit
from unisi.tables import (
    Table, PandaTable, get_chunk, accept_cell_value, delete_table_row,
    append_table_row, delete_panda_row, accept_panda_cell, append_panda_row,
)


def plain(rows):
    """Unwrap a (possibly ChangedProxy-wrapped) rows collection into plain
    nested lists, so tests can compare with == instead of caring whether
    reactivity is active."""
    return [[cell for cell in row] for row in rows]


# ──────────────────────────────────────────────────────────────────────── #
#  Table -- non-persistent (no id=...)                                     #
# ──────────────────────────────────────────────────────────────────────── #

class TestTableNonPersistentDefaults:
    def test_bare_defaults(self):
        t = Table('t')
        assert t.headers == []
        assert t.type == 'table'
        assert t.value is None
        assert t.rows == []
        assert t.editing is False
        assert t.dense is True

    def test_explicit_headers_and_rows_are_respected(self):
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', 30]])
        assert t.headers == ['Name', 'Age']
        assert plain(t.rows) == [['Alice', 30]]

    def test_edit_true_by_default_wires_row_handlers(self):
        t = Table('t')
        assert t.delete is delete_table_row
        assert t.append is append_table_row
        assert t.modify is accept_cell_value

    def test_edit_false_leaves_row_handlers_unset(self):
        t = Table('t', edit=False)
        assert not hasattr(t, 'delete')
        assert not hasattr(t, 'append')
        assert not hasattr(t, 'modify')

    def test_is_base_table_list_is_falsy_for_non_persistent_table(self):
        t = Table('t')
        assert not t.is_base_table_list

    def test_panda_property_is_none_for_plain_table(self):
        t = Table('t')
        assert t.panda is None


class TestTableSelectedListAndCleanSelection:
    @pytest.mark.parametrize('value,expected', [
        (None, []),
        (2, [2]),
        (0, [0]),
        ([1, 2], [1, 2]),
        ([], []),
    ])
    def test_selected_list_normalises_value(self, value, expected):
        t = Table('t', value=value)
        assert t.selected_list == expected

    def test_clean_selection_resets_list_value_to_empty_list(self):
        t = Table('t', value=[1, 2])
        t.clean_selection()
        assert t.value == []

    def test_clean_selection_resets_scalar_value_to_none(self):
        t = Table('t', value=5)
        t.clean_selection()
        assert t.value is None

    def test_clean_selection_returns_self(self):
        t = Table('t')
        assert t.clean_selection() is t


class TestTableCompactView:
    def test_uses_all_rows_when_nothing_selected_and_table_is_small(self):
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', 30], ['Bob', 40]])
        assert t.compact_view == 't : Name: Alice,Age: 30;Name: Bob,Age: 40'

    def test_uses_only_selected_rows_when_a_selection_exists(self):
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', 30], ['Bob', 40]], value=1)
        assert t.compact_view == 't : Name: Bob,Age: 40'

    def test_large_table_with_no_selection_yields_empty_rows_section(self):
        from unisi.tables import max_len_rows4llm
        rows = [[f'name{i}', i] for i in range(max_len_rows4llm)]
        t = Table('t', headers=['Name', 'Age'], rows=rows)
        assert t.compact_view == 't : '


# ──────────────────────────────────────────────────────────────────────── #
#  accept_cell_value (non-persistent path)                                 #
# ──────────────────────────────────────────────────────────────────────── #

class TestAcceptCellValueNonPersistent:
    def test_numeric_string_is_coerced_to_float(self):
        t = Table('t', headers=['N'], rows=[[0]])
        t.modify(t, {'delta': 0, 'cell': 0, 'value': '42'})
        assert plain(t.rows) == [[42.0]]

    def test_bool_value_is_not_coerced_to_float(self):
        t = Table('t', headers=['Flag'], rows=[[False]])
        t.modify(t, {'delta': 0, 'cell': 0, 'value': True})
        assert plain(t.rows) == [[True]]

    def test_non_numeric_string_is_kept_as_is(self):
        t = Table('t', headers=['Name'], rows=[['']])
        t.modify(t, {'delta': 0, 'cell': 0, 'value': 'hello'})
        assert plain(t.rows) == [['hello']]

    def test_writes_the_correct_row_and_column(self):
        t = Table('t', headers=['A', 'B'], rows=[[1, 2], [3, 4]])
        t.modify(t, {'delta': 1, 'cell': 1, 'value': '99'})
        assert plain(t.rows) == [[1, 2], [3, 99.0]]


# ──────────────────────────────────────────────────────────────────────── #
#  delete_table_row (non-persistent path)                                  #
# ──────────────────────────────────────────────────────────────────────── #

class TestDeleteTableRowNonPersistent:
    def test_deletes_a_list_of_selected_rows(self):
        t = Table('t', headers=['N'], rows=[[0], [1], [2]])
        t.value = [0, 2]
        t.delete(t, [0, 2])
        assert plain(t.rows) == [[1]]
        assert t.value == []

    def test_deletes_a_single_selected_row(self):
        t = Table('t', headers=['N'], rows=[[0], [1], [2]])
        t.value = 1
        t.delete(t, 1)
        assert plain(t.rows) == [[0], [2]]
        assert t.value is None

    def test_deletes_row_at_index_zero(self):
        """Row index 0 is falsy -- make sure it isn't mistaken for 'nothing
        selected' anywhere in the delete path."""
        t = Table('t', headers=['N'], rows=[[10], [20]])
        t.value = 0
        t.delete(t, 0)
        assert plain(t.rows) == [[20]]

    def test_none_value_is_a_noop(self):
        t = Table('t', headers=['N'], rows=[[0], [1]])
        t.delete(t, None)
        assert plain(t.rows) == [[0], [1]]

    def test_empty_list_value_is_a_noop(self):
        t = Table('t', headers=['N'], rows=[[0], [1]])
        t.delete(t, [])
        assert plain(t.rows) == [[0], [1]]

    def test_regression_delete_uses_the_argument_not_stale_table_value(self):
        """Regression: the guard used to check table.selected_list (i.e.
        table.value) instead of the `value` the handler was actually asked
        to delete. A caller that passes an explicit value while table.value
        is still None/stale (e.g. a standalone per-row delete icon with no
        separate select step) used to silently delete nothing at all."""
        t = Table('t', headers=['N'], rows=[[0], [1], [2]])
        assert t.value is None  # deliberately NOT synced with what we delete
        t.delete(t, [0, 1])
        assert plain(t.rows) == [[2]]

    def test_regression_delete_single_row_ignores_stale_table_value(self):
        t = Table('t', headers=['N'], rows=[[10], [20], [30]])
        assert t.value is None
        t.delete(t, 1)
        assert plain(t.rows) == [[10], [30]]


# ──────────────────────────────────────────────────────────────────────── #
#  append_table_row (non-persistent path)                                  #
# ──────────────────────────────────────────────────────────────────────── #

class TestAppendTableRowNonPersistent:
    def test_appends_a_row_of_nones_sized_to_headers(self):
        t = Table('t', headers=['A', 'B', 'C'], rows=[])
        new_row = t.append(t, '')
        assert new_row == [None, None, None]
        assert plain(t.rows) == [[None, None, None]]

    def test_appended_row_is_the_same_object_stored_in_rows(self):
        t = Table('t', headers=['A'], rows=[])
        new_row = t.append(t, '')
        new_row[0] = 'written directly'
        assert plain(t.rows) == [['written directly']]


# ──────────────────────────────────────────────────────────────────────── #
#  PandaTable / panda-backed Table                                         #
# ──────────────────────────────────────────────────────────────────────── #

def make_df():
    return pd.DataFrame({'name': ['Alice', 'Bob', 'Carol'], 'age': [30, 40, 50]})


class TestPandaTableConstruction:
    def test_headers_default_to_prettified_column_names(self):
        df = pd.DataFrame({'first_name': ['Alice']})
        t = Table('t', panda=df)
        assert t.headers == ['First name']

    def test_fix_headers_false_keeps_raw_column_names(self):
        df = pd.DataFrame({'first_name': ['Alice']})
        t = PandaTable('t', panda=df, fix_headers=False)
        assert t.headers == ['first_name']

    def test_rows_mirror_the_dataframe(self):
        t = Table('t', panda=make_df())
        assert plain(t.rows) == [['Alice', 30], ['Bob', 40], ['Carol', 50]]

    def test_panda_property_returns_the_live_dataframe(self):
        df = make_df()
        t = Table('t', panda=df)
        assert t.panda is df

    def test_missing_panda_argument_raises(self):
        with pytest.raises(Exception):
            PandaTable('t')

    def test_edit_true_by_default_wires_panda_row_handlers(self):
        t = Table('t', panda=make_df())
        assert t.delete is delete_panda_row
        assert t.append is append_panda_row
        assert t.modify is accept_panda_cell

    def test_type_is_table(self):
        t = Table('t', panda=make_df())
        assert t.type == 'table'


class TestDeletePandaRow:
    def test_deletes_a_single_row_by_position(self):
        t = Table('t', panda=make_df())
        t.delete(t, 1)
        assert plain(t.rows) == [['Alice', 30], ['Carol', 50]]
        assert list(t.panda['name']) == ['Alice', 'Carol']

    def test_deletes_multiple_rows(self):
        t = Table('t', panda=make_df())
        t.delete(t, [0, 2])
        assert plain(t.rows) == [['Bob', 40]]

    def test_out_of_range_index_raises_value_error(self):
        t = Table('t', panda=make_df())
        with pytest.raises(ValueError):
            t.delete(t, 99)

    def test_regression_append_after_delete_does_not_corrupt_existing_row(self):
        """Regression: deleting a row used to leave the DataFrame's index
        non-contiguous (reset_index was commented out); the next append
        (`df.loc[len(df)] = new_row`) could then collide with an existing
        label and silently overwrite that row's data instead of adding a
        new one. This is the exact Alice/Bob/Carol scenario that surfaced
        the bug."""
        t = Table('t', panda=make_df())
        t.delete(t, 1)  # remove Bob (the middle row)
        t.append(t, '')
        assert len(t.panda) == 3
        assert list(t.panda['name']) == ['Alice', 'Carol', None]
        assert list(t.panda.index) == [0, 1, 2]  # contiguous again

    def test_regression_reset_index_does_not_leave_a_stray_index_column(self):
        """reset_index(drop=True, ...) matters: without drop=True, pandas
        inserts the old index as a brand-new 'index' column, which would
        silently shift every column position relative to `self.headers`
        (accept_panda_cell addresses cells positionally)."""
        t = Table('t', panda=make_df())
        t.delete(t, 0)
        assert list(t.panda.columns) == ['name', 'age']

    def test_multiple_delete_append_cycles_preserve_row_count_and_data(self):
        t = Table('t', panda=make_df())
        t.delete(t, 0)                         # -> Bob, Carol
        t.append(t, '')                        # -> Bob, Carol, None
        t.modify(t, {'delta': 2, 'cell': 0, 'value': 'Dave'})
        t.delete(t, 1)                         # remove Carol -> Bob, Dave
        assert list(t.panda['name']) == ['Bob', 'Dave']
        assert len(t.panda) == 2


class TestAcceptPandaCell:
    def test_updates_dataframe_and_rows_together(self):
        t = Table('t', panda=make_df())
        t.modify(t, {'delta': 0, 'cell': 1, 'value': '99'})
        assert t.panda.iat[0, 1] == 99.0
        assert plain(t.rows)[0] == ['Alice', 99.0]

    def test_bool_value_not_coerced(self):
        df = pd.DataFrame({'active': [False, False]})
        t = Table('t', panda=df)
        t.modify(t, {'delta': 0, 'cell': 0, 'value': True})
        assert bool(t.panda.iat[0, 0]) is True


class TestAppendPandaRow:
    def test_appends_a_row_of_nones_to_both_dataframe_and_rows(self):
        t = Table('t', panda=make_df())
        new_row = t.append(t, '')
        assert new_row == [None, None]
        assert len(t.panda) == 4
        assert plain(t.rows)[-1] == [None, None]


# ──────────────────────────────────────────────────────────────────────── #
#  Table -- persistent (id=...), unlinked                                  #
# ──────────────────────────────────────────────────────────────────────── #

def set_cell(table, delta, cell, value):
    """Write a cell through the real modify path (Dblist.update_cell), the
    only way a write actually reaches a persistent table's database -- see
    the module docstring for why `table.rows[i][j] = value` doesn't."""
    table.modify(table, {'delta': delta, 'cell': cell, 'value': value, 'id': None})


class TestTablePersistentBasic:
    def test_construction_wires_the_db_and_get_handler(self, memdb):
        t = Table('People', id='People', fields={'name': str, 'age': int})
        assert hasattr(t, 'id')
        assert t.get is get_chunk
        assert t.filter is False       # no link -> filter defaults off
        assert t.ids is False
        assert t.search == ''

    def test_headers_auto_generated_from_fields_when_not_given(self, memdb):
        t = Table('People', id='People', fields={'name': str, 'age': int})
        assert t.headers == ['Name', 'Age']

    def test_explicit_headers_are_kept(self, memdb):
        t = Table('People', id='People', fields={'name': str}, headers=['Full Name'])
        assert t.headers == ['Full Name']

    def test_raises_without_a_configured_db(self, fake_user):
        assert Unishare.db is None
        with pytest.raises(AssertionError):
            Table('People', id='People', fields={'name': str})

    def test_ids_option_without_persistence_is_rejected(self):
        with pytest.raises(ValueError):
            Table('t', ids=True)

    def test_append_then_modify_then_read_round_trips(self, memdb):
        t = Table('People', id='People', fields={'name': str, 'age': int})
        new_row = t.append(t, '')
        assert new_row[-1] is not None  # got a real DB id
        set_cell(t, 0, 0, 'Alice')
        set_cell(t, 0, 1, '30')
        assert plain(t.rows) == [['Alice', 30.0, new_row[-1]]]

    def test_delete_removes_the_persisted_row(self, memdb):
        t = Table('People', id='People', fields={'name': str})
        t.append(t, '')
        t.append(t, '')
        t.value = 0
        t.delete(t, 0)
        assert len(list(t.rows)) == 1

    def test_search_handler_is_registered_via_unishare_handle(self, memdb, fake_user):
        t = Table('People', id='People', fields={'name': str})
        assert (t, 'search') in fake_user.handlers

    def test_search_filters_rows_and_clears_selection(self, memdb, fake_user):
        t = Table('People', id='People', fields={'name': str})
        t.append(t, ''); set_cell(t, 0, 0, 'Alice')
        t.append(t, ''); set_cell(t, 1, 0, 'Bob')
        t.value = [0, 1]

        search_handler = fake_user.handlers[(t, 'search')]
        search_handler(t, 'Ali')

        assert [row[0] for row in t.rows] == ['Alice']
        assert t.value == []  # clean_selection() ran

    def test_clearing_search_restores_full_list(self, memdb, fake_user):
        t = Table('People', id='People', fields={'name': str})
        t.append(t, ''); set_cell(t, 0, 0, 'Alice')
        t.append(t, ''); set_cell(t, 1, 0, 'Bob')

        search_handler = fake_user.handlers[(t, 'search')]
        search_handler(t, 'Ali')
        search_handler(t, '')

        assert sorted(row[0] for row in t.rows) == ['Alice', 'Bob']

    def test_get_chunk_returns_a_delta_update_payload(self, memdb):
        t = Table('People', id='People', fields={'name': str})
        t.append(t, ''); set_cell(t, 0, 0, 'Alice')
        t.append(t, ''); set_cell(t, 1, 0, 'Bob')
        chunk = t.get(t, 0)
        assert chunk['update'] == 'updates'
        assert 'index' in chunk and 'data' in chunk

    def test_is_base_table_list_true_for_the_unfiltered_full_list(self, memdb):
        t = Table('People', id='People', fields={'name': str})
        assert t.is_base_table_list is True

    def test_link_to_a_non_persistent_table_raises(self, memdb):
        not_persistent = Table('t')  # no id= -> not persistent
        with pytest.raises(AttributeError):
            Table('Orders', id='Orders', fields={'item': str}, link=not_persistent)


class TestCalcHeaders:
    def test_plain_persistent_table_has_no_id_column(self, memdb):
        t = Table('People', id='People', fields={'name': str})
        assert t.headers == ['Name']

    def test_ids_true_appends_id_column(self, memdb):
        t = Table('People', id='People', fields={'name': str}, ids=True)
        assert t.headers == ['Name', 'ID']

    def test_filter_true_without_ids_appends_excluded_id_marker(self, memdb):
        t = Table('People', id='People', fields={'name': str}, filter=True)
        from unisi.tables import exclude_mark
        assert t.headers == ['Name', exclude_mark + 'ID']

    def test_ids_true_takes_priority_over_filter_exclude_marker(self, memdb):
        t = Table('People', id='People', fields={'name': str}, ids=True, filter=True)
        assert 'ID' in t.headers
        from unisi.tables import exclude_mark
        assert exclude_mark + 'ID' not in t.headers

    def test_recalculating_headers_is_idempotent_for_a_plain_table(self, memdb):
        t = Table('People', id='People', fields={'name': str})
        before = t.headers[:]
        t.calc_headers()
        assert t.headers == before

    def test_regression_filter_true_without_a_link_does_not_crash(self, memdb):
        """Regression: calc_headers()'s relation-header step unconditionally
        read self.link whenever self.filter was truthy, but self.link only
        ever gets set when the table was built with link=... . A table
        built with an explicit filter=True and no link= crashed with
        AttributeError as soon as __init__ called calc_headers()."""
        from unisi.tables import exclude_mark
        t = Table('People', id='People', fields={'name': str}, filter=True)
        assert t.headers == ['Name', exclude_mark + 'ID']

    def test_ids_true_on_a_linked_table_appends_a_relation_id_column_too(self, memdb):
        users = Table('Users', id='Users', fields={'name': str})
        orders = Table('Orders', id='Orders', fields={'item': str},
                        link=[users, {'qty': int}], ids=True)
        from unisi.tables import relation_mark
        assert 'ID' in orders.headers
        assert relation_mark + 'ID' in orders.headers


# ──────────────────────────────────────────────────────────────────────── #
#  Table -- persistent, linked (many-to-one)                               #
# ──────────────────────────────────────────────────────────────────────── #

def make_users(memdb, names):
    users = Table('Users', id='Users', fields={'name': str})
    for i, name in enumerate(names):
        users.append(users, '')
        set_cell(users, i, 0, name)
    return users


class TestTableLinkedManyToOne:
    def test_link_is_empty_rel_fields_dict(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        assert orders.link == {}

    def test_filter_defaults_true_and_headers_include_link_id_and_excluded_id(self, memdb):
        from unisi.tables import exclude_mark
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        assert orders.filter is True
        assert orders.headers == ['Item', 'Link id', exclude_mark + 'ID']

    def test_registers_changed_handler_on_the_link_table(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        assert (users, 'changed') in fake_user.handlers
        assert (orders, 'filter') in fake_user.handlers
        assert (orders, 'changed') in fake_user.handlers

    def test_no_master_selected_means_no_child_rows(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        assert list(orders.rows) == []

    def test_selecting_a_master_row_filters_to_its_children(self, memdb):
        users = make_users(memdb, ['Alice', 'Bob'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        orders.append(orders, '')  # unlinked, nobody selected yet
        set_cell(orders, 0, 0, 'Unlinked')

        users.value = 0  # select Alice
        orders.__link_table_selection_changed__(users, 0)
        orders.append(orders, '')
        set_cell(orders, 0, 0, 'Widget')  # position 0 of the now-filtered view

        assert [row[0] for row in orders.rows] == ['Widget']

    def test_append_while_master_selected_stamps_the_fk(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)

        new_row = orders.append(orders, '')
        link_id_col = orders.rows.dbtable.node_columns.index(orders.rows.dbtable.LINK_ID)
        assert new_row[link_id_col] == users.rows[0][-1]

    def test_append_with_no_master_selected_leaves_fk_unset(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        new_row = orders.append(orders, '')
        link_id_col = orders.rows.dbtable.node_columns.index(orders.rows.dbtable.LINK_ID)
        assert new_row[link_id_col] is None

    def test_delete_clears_the_fk_rather_than_removing_the_row(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        orders.append(orders, '')

        orders.value = 0
        orders.delete(orders, 0)

        assert list(orders.rows) == []  # no longer shows for Alice
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)
        assert len(list(orders.rows)) == 1  # the order itself still exists, just unlinked

    def test_switching_filter_off_shows_full_list_and_matching_ids_as_value(self, memdb, fake_user):
        users = make_users(memdb, ['Alice', 'Bob'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        alice_order = orders.append(orders, '')

        users.value = None
        orders.__link_table_selection_changed__(users, None)
        another_order = orders.append(orders, '')  # unlinked

        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)

        assert orders.filter is False
        assert len(list(orders.rows)) == 2  # base/full list, not filtered
        assert orders.value == [alice_order[-1]]  # only Alice's order id

    def test_changed_selection_sets_fk_when_editing_and_unfiltered(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        orders.editing = True
        orders.append(orders, '')  # created with nobody selected -> unlinked
        set_cell(orders, 0, 0, 'Widget')

        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)  # unfiltered, so selecting a row means "link it"

        changed_handler = fake_user.handlers[(orders, 'changed')]
        orders.value = None
        changed_handler(orders, 0)  # select order at position 0 -> links it to Alice

        filter_handler(orders, True)  # switch back to Alice's filtered view
        assert [row[0] for row in orders.rows] == ['Widget']

    def test_changed_deselection_clears_the_fk(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        orders.editing = True
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        orders.append(orders, '')  # linked to Alice immediately
        set_cell(orders, 0, 0, 'Widget')

        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)
        changed_handler = fake_user.handlers[(orders, 'changed')]
        orders.value = 0             # currently selected (linked) row
        changed_handler(orders, None)  # deselect -> clears the fk

        filter_handler(orders, True)
        assert list(orders.rows) == []  # no longer shows for Alice

    def test_search_on_a_linked_table_delegates_to_the_link_refresh(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        orders.append(orders, '')
        set_cell(orders, 0, 0, 'Widget')
        orders.append(orders, '')
        set_cell(orders, 1, 0, 'Gadget')

        search_handler = fake_user.handlers[(orders, 'search')]
        search_handler(orders, 'Widg')

        assert [row[0] for row in orders.rows] == ['Widget']

    def test_search_while_unfiltered_uses_search_rows_on_the_base_table(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        orders.append(orders, ''); set_cell(orders, 0, 0, 'Widget')
        orders.append(orders, ''); set_cell(orders, 1, 0, 'Gadget')

        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)
        search_handler = fake_user.handlers[(orders, 'search')]
        search_handler(orders, 'Widg')

        assert [row[0] for row in orders.rows] == ['Widget']

    def test_changed_selection_returns_warning_when_not_editing(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=users)
        orders.editing = False
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)

        changed_handler = fake_user.handlers[(orders, 'changed')]
        result = changed_handler(orders, 0)

        assert result.type == 'warning'
        assert result.value == 'The linked table is not in edit mode'


# ──────────────────────────────────────────────────────────────────────── #
#  Table -- persistent, linked (many-to-many)                              #
# ──────────────────────────────────────────────────────────────────────── #

class TestTableLinkedManyToMany:
    def test_link_holds_the_relation_fields_dict(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        assert 'qty' in orders.link

    def test_headers_include_relation_field(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        assert any(h.endswith('Qty') for h in orders.headers)

    def test_append_while_master_selected_creates_a_junction_link(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)

        orders.append(orders, '')
        set_cell(orders, 0, 0, 'Widget')

        assert [row[0] for row in orders.rows] == ['Widget']  # visible under Alice's filter

    def test_append_with_no_master_selected_creates_no_link(self, memdb):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        orders.append(orders, '')
        set_cell(orders, 0, 0, 'Unlinked Widget')

        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        assert list(orders.rows) == []  # nothing linked to Alice

    def test_delete_removes_the_junction_row_not_the_order(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        orders.append(orders, '')

        orders.value = 0
        orders.delete(orders, 0)

        assert list(orders.rows) == []  # unlinked from Alice
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)
        assert len(list(orders.rows)) == 1  # the order itself still exists

    def test_regression_selecting_a_child_row_links_it_via_real_ids_not_indices(self, memdb, fake_user):
        """Regression: changed_selection_causes__changing_links' m2m branch
        used to pass raw row-selection *indices* straight to
        add_links/delete_links, which build SQL directly from them and
        expect actual database ids. Selecting a child row to link it to the
        current master used to crash with a FOREIGN KEY constraint error
        (ids start at 1, indices at 0, so the "id" almost never exists)."""
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        orders.editing = True

        orders.append(orders, '')  # unlinked order, created with nobody selected
        set_cell(orders, 0, 0, 'Widget')

        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)  # unfiltered: base list, select-to-link mode

        changed_handler = fake_user.handlers[(orders, 'changed')]
        orders.value = None
        changed_handler(orders, 0)  # select the Widget row -> should link, not crash with a FK error

        filter_handler(orders, True)  # back to filtered-by-Alice view
        assert [row[0] for row in orders.rows] == ['Widget']

    def test_regression_deselecting_a_child_row_unlinks_it_via_real_ids(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        orders.editing = True
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        orders.append(orders, '')  # linked to Alice immediately

        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)
        changed_handler = fake_user.handlers[(orders, 'changed')]
        orders.value = 0
        changed_handler(orders, None)  # deselect -> unlink

        filter_handler(orders, True)
        assert list(orders.rows) == []

    def test_changed_selection_returns_warning_when_not_editing(self, memdb, fake_user):
        users = make_users(memdb, ['Alice'])
        orders = Table('Orders', id='Orders', fields={'item': str}, link=[users, {'qty': int}])
        orders.editing = False
        users.value = 0
        orders.__link_table_selection_changed__(users, 0)
        filter_handler = fake_user.handlers[(orders, 'filter')]
        filter_handler(orders, False)

        changed_handler = fake_user.handlers[(orders, 'changed')]
        result = changed_handler(orders, 0)

        assert result.type == 'warning'


# ──────────────────────────────────────────────────────────────────────── #
#  Table.emit (LLM auto-fill)                                              #
# ──────────────────────────────────────────────────────────────────────── #

class TestTableEmit:
    @pytest.mark.asyncio
    async def test_noop_when_llm_off(self, llm_off, fake_get_property):
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', None]], value=0, llm=True)
        t._llm_dependencies = {'Age': True}
        result = await t.emit()
        assert result is None
        assert fake_get_property.calls == []

    @pytest.mark.asyncio
    async def test_noop_when_llm_attribute_not_set(self, llm_on, fake_get_property):
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', None]], value=0)
        result = await t.emit()
        assert result is None
        assert fake_get_property.calls == []

    @pytest.mark.asyncio
    async def test_fills_missing_field_for_selected_row_using_deps_true(self, llm_on, fake_get_property):
        fake_get_property.result = 42
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', None]], value=0, llm=False)
        t._llm_dependencies = {'Age': True}  # True -> whole row as context
        result = await t.emit()
        assert result is t
        assert plain(t.rows)[0][1] == 42

    @pytest.mark.asyncio
    async def test_does_not_touch_a_field_that_already_has_a_value(self, llm_on, fake_get_property):
        t = Table('t', headers=['Name', 'Age'], rows=[['Alice', 30]], value=0, llm=False)
        t._llm_dependencies = {'Age': True}
        await t.emit()
        assert fake_get_property.calls == []
        assert plain(t.rows)[0][1] == 30

    @pytest.mark.asyncio
    async def test_only_processes_selected_rows(self, llm_on, fake_get_property):
        fake_get_property.result = 99
        t = Table('t', headers=['Name', 'Age'],
                  rows=[['Alice', None], ['Bob', None]], value=0, llm=False)
        t._llm_dependencies = {'Age': True}
        await t.emit()
        rows = plain(t.rows)
        assert rows[0][1] == 99
        assert rows[1][1] is None  # Bob wasn't selected, left untouched

    @pytest.mark.asyncio
    async def test_explicit_dependency_list_builds_context_from_named_fields(self, llm_on, fake_get_property):
        t = Table('t', headers=['Name', 'Country', 'Capital'],
                  rows=[['Alice', 'France', None]], value=0, llm=False)
        t._llm_dependencies = {'Capital': ['Country']}
        await t.emit()
        name, context, type_, options = fake_get_property.calls[0]
        assert name == 'Capital'
        assert 'France' in context

    @pytest.mark.asyncio
    async def test_exact_mode_skips_field_with_an_unfilled_explicit_dependency(self, llm_on, fake_get_property):
        t = Table('t', headers=['Name', 'Country', 'Capital'],
                  rows=[['Alice', None, None]], value=0, llm=True)  # exact mode
        t._llm_dependencies = {'Capital': ['Country']}  # Country isn't filled in
        result = await t.emit()
        assert result is None
        assert fake_get_property.calls == []

    @pytest.mark.asyncio
    async def test_regression_dependency_as_unit_uses_its_name_and_value(self, llm_on, fake_get_property):
        """Regression: `value = values.get(dep, None)` used to run before
        dep's type was even inspected. `values` is keyed by this table's
        own column headers, so for a Unit-typed dep (an external widget,
        not a column of this table) that lookup was always None -- the
        isinstance(dep, Unit) branch that reads dep.value directly was
        dead code, so a Unit dependency's actual value was never used."""
        dep_unit = Unit('Country', value='France')
        t = Table('t', headers=['Name', 'Capital'], rows=[['Alice', None]], value=0, llm=False)
        t._llm_dependencies = {'Capital': [dep_unit]}
        await t.emit()
        name, context, type_, options = fake_get_property.calls[0]
        assert 'Country' in context and 'France' in context

    @pytest.mark.asyncio
    async def test_regression_invalid_dependency_type_always_raises(self, llm_on, fake_get_property):
        """Regression: for the same reason as above, an invalid (neither
        str nor Unit) dependency only ever raised if `values.get(dep)`
        happened to find a match -- for a genuinely invalid dep (e.g. an
        int) that's never the case, so the "raise AttributeError" meant to
        guard against exactly this was itself unreachable and the bad
        dependency was silently skipped instead."""
        t = Table('t', headers=['Name', 'Capital'], rows=[['Alice', None]], value=0, llm=False)
        t._llm_dependencies = {'Capital': [123]}  # neither a str nor a Unit
        with pytest.raises(AttributeError):
            await t.emit()

