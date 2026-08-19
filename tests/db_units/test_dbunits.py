# Copyright © 2024 UNISI Tech. All rights reserved.
"""
Unit tests for unisi/dbunits.py -- the Dblist paginated proxy-list class,
plus the module-level at_iter() helper.

Organisation
────────────
  TestAtIter              - the at_iter() helper in isolation
  TestConstruction        - Dblist.__init__ contract
  TestBasicAccess         - __len__/__getitem__/slicing/__iter__/__str__
  TestSetItem             - __setitem__ and its guards
  TestDelItem             - __delitem__, incl. the chunk-borrow-on-delete path
  TestRemovePopInsert     - remove()/pop()/insert()
  TestAppendExtend        - append()/extend(), incl. chunk-boundary regressions
  TestDirectDbtableBypass - self-healing when Dbtable is mutated directly
  TestUpdateCell          - update_cell() for node fields and relation fields
  TestChunkingStress      - randomised sequences of mutations vs. a plain
                             Python list reference model

Regression tests are marked "Regression:" in their docstring. Several of
these were found empirically (not from reading the code) by running
randomised operation sequences against a reference list and diffing --
TestChunkingStress is a permanent, deterministic version of that same
exercise.
"""
import random

import pytest

from unisi.dbunits import Dblist, at_iter, dbupdates


# ────────────────────────────────────────────────────────────────────────── #
#  at_iter                                                                    #
# ────────────────────────────────────────────────────────────────────────── #

class TestAtIter:
    def test_returns_element_at_position(self):
        assert at_iter(iter(["a", "b", "c"]), 1) == "b"

    def test_position_zero(self):
        assert at_iter(iter(["a", "b", "c"]), 0) == "a"

    def test_out_of_range_raises_index_error(self):
        with pytest.raises(IndexError):
            at_iter(iter(["a", "b"]), 5)

    def test_negative_times_always_raises(self):
        """enumerate() never produces a negative index, so a negative
        `times` can never match -- documents the existing contract rather
        than asserting it's the ideal API."""
        with pytest.raises(IndexError):
            at_iter(iter(["a", "b", "c"]), -1)

    def test_works_with_dict_key_iteration(self):
        """index2node_relation() relies on exactly this: iterating a
        dict's keys by position."""
        d = {"name": 1, "age": 2, "note": 3}
        assert at_iter(iter(d), 1) == "age"


# ────────────────────────────────────────────────────────────────────────── #
#  Construction                                                               #
# ────────────────────────────────────────────────────────────────────────── #

class TestConstruction:
    def test_live_mode_requires_init_list_or_cache(self, db):
        t = db.create_table("T", {"name": "TEXT"})
        with pytest.raises(AttributeError):
            Dblist(t)

    def test_live_mode_with_init_list(self, db):
        t = db.create_table("T", {"name": "TEXT"})
        lst = Dblist(t, init_list=[])
        assert len(lst) == 0  # driven by dbtable.length, not init_list's own size

    def test_cache_mode_truncates_to_limit(self, db):
        t = db.create_table("T", {"name": "TEXT"}, limit=2)
        lst = Dblist(t, cache=[["a"], ["b"], ["c"], ["d"]])
        assert len(lst) == 4  # __len__ uses len(cache) in cache-mode...
        assert lst.delta_list[0] == [["a"], ["b"]]  # ...but delta_list[0] is capped


# ────────────────────────────────────────────────────────────────────────── #
#  Basic access                                                               #
# ────────────────────────────────────────────────────────────────────────── #

class TestBasicAccess:
    def test_len_live_mode_tracks_dbtable_length(self, db, table):
        assert len(table.list) == 0
        table.list.append(["Alice", 30])
        assert len(table.list) == 1

    def test_len_cache_mode_tracks_cache(self, db, table):
        lst = Dblist(table, cache=[["a"], ["b"]])
        assert len(lst) == 2

    def test_getitem_positive_and_negative_index(self, db, table):
        table.list.append(["Alice", 30])
        table.list.append(["Bob", 25])
        assert table.list[0][0] == "Alice"
        assert table.list[-1][0] == "Bob"
        assert table.list[-2][0] == "Alice"

    def test_getitem_out_of_range_raises_index_error(self, db, table):
        table.list.append(["Alice", 30])
        with pytest.raises(IndexError):
            table.list[5]
        with pytest.raises(IndexError):
            table.list[-5]

    def test_getitem_slice_positive(self, db, table):
        for i in range(5):
            table.list.append([f"n{i}", i])
        assert [r[0] for r in table.list[1:3]] == ["n1", "n2"]

    def test_getitem_slice_negative_and_step(self, db, table):
        for i in range(5):
            table.list.append([f"n{i}", i])
        assert [r[0] for r in table.list[-3:]] == ["n2", "n3", "n4"]
        assert [r[0] for r in table.list[::2]] == ["n0", "n2", "n4"]

    def test_cache_mode_getitem(self, db, table):
        lst = Dblist(table, cache=[["a"], ["b"], ["c"]])
        assert lst[1] == ["b"]
        assert lst[-1] == ["c"]

    def test_iter_produces_independent_generators(self, db, table):
        """Nested `for i in lst: for j in lst: ...` must not interfere --
        each __iter__() call is documented to return a fresh generator."""
        for i in range(3):
            table.list.append([f"n{i}", i])

        outer_seen = []
        for i, row_i in enumerate(table.list):
            inner_seen = [row_j[0] for row_j in table.list]
            outer_seen.append((row_i[0], inner_seen))

        assert outer_seen == [
            ("n0", ["n0", "n1", "n2"]),
            ("n1", ["n0", "n1", "n2"]),
            ("n2", ["n0", "n1", "n2"]),
        ]

    def test_str_and_getstate(self, db, table):
        table.list.append(["Alice", 30])
        state = table.list.__getstate__()
        assert state["length"] == 1
        assert state["limit"] == table.limit
        assert state["data"] == [["Alice", 30, 1]]
        assert str(table.list) == str(state)


# ────────────────────────────────────────────────────────────────────────── #
#  __setitem__                                                                #
# ────────────────────────────────────────────────────────────────────────── #

class TestSetItem:
    def test_updates_cell_and_persists_to_db(self, db, table):
        row = table.list.append(["Alice", 30])
        table.list[0] = ["Alicia", 31, row[-1]]
        assert table.list[0] == ["Alicia", 31, row[-1]]
        assert table.read_rows()[0] == ["Alicia", 31, row[-1]]  # persisted, not just cached

    def test_negative_index(self, db, table):
        table.list.append(["Alice", 30])
        row2 = table.list.append(["Bob", 25])
        table.list[-1] = ["Bobby", 26, row2[-1]]
        assert table.list[1] == ["Bobby", 26, row2[-1]]

    def test_rejects_slice(self, db, table):
        table.list.append(["Alice", 30])
        with pytest.raises(NotImplementedError):
            table.list[0:1] = [["X", 1, 1]]

    def test_rejects_id_change(self, db, table):
        row = table.list.append(["Alice", 30])
        with pytest.raises(ValueError):
            table.list[0] = ["Alicia", 31, row[-1] + 999]

    def test_cache_mode_is_read_only(self, db, table):
        lst = Dblist(table, cache=[["a"], ["b"]])
        with pytest.raises(TypeError):
            lst[0] = ["z"]

    def test_out_of_range_raises_index_error(self, db, table):
        with pytest.raises(IndexError):
            table.list[0] = ["x", 1, 1]


# ────────────────────────────────────────────────────────────────────────── #
#  __delitem__                                                                #
# ────────────────────────────────────────────────────────────────────────── #

class TestDelItem:
    def test_deletes_by_position_and_persists(self, db, table):
        table.list.append(["Alice", 30])
        table.list.append(["Bob", 25])
        del table.list[0]
        assert [r[0] for r in table.list] == ["Bob"]
        assert table.read_rows() == [["Bob", 25, 2]]  # gone from the DB too

    def test_deletes_the_correct_row_when_ids_have_gaps(self, db, table):
        """The critical invariant this class documents: the DB receives
        the row's actual ID, never the list offset, so a deletion earlier
        in the list can't cause a *later* delete-by-offset to hit the
        wrong row."""
        table.list.append(["Alice", 30])   # ID 1
        table.list.append(["Bob", 25])     # ID 2
        table.list.append(["Carol", 40])   # ID 3
        del table.list[0]                  # removes Alice (ID 1); Bob shifts to offset 0
        del table.list[0]                  # must remove Bob (ID 2), NOT re-hit ID 1
        assert [r[0] for r in table.list] == ["Carol"]
        assert table.read_rows() == [["Carol", 40, 3]]

    def test_negative_index(self, db, table):
        table.list.append(["Alice", 30])
        table.list.append(["Bob", 25])
        del table.list[-1]
        assert [r[0] for r in table.list] == ["Alice"]

    def test_slice_deletion(self, db, table):
        for i in range(5):
            table.list.append([f"n{i}", i])
        del table.list[1:3]
        assert [r[0] for r in table.list] == ["n0", "n3", "n4"]

    def test_out_of_range_raises_index_error(self, db, table):
        with pytest.raises(IndexError):
            del table.list[0]

    def test_borrows_from_next_chunk_when_a_full_chunk_shrinks(self, db):
        """
        When a chunk that was exactly at `limit` loses one row, __delitem__
        borrows the first row of the next cached chunk to keep offsets
        aligned, then evicts that next chunk (its own offsets are now
        stale by one). This test pins that exact mechanism down at a small
        limit where it's easy to see chunk boundaries move.
        """
        t = db.create_table("T", {"name": "TEXT"}, limit=3)
        for i in range(6):
            t.list.append([f"n{i}"])
        # append() only patches a chunk it already knows the full contents
        # of (see its docstring); the second chunk was never read yet, so
        # it isn't cached until something actually reads from it -- do
        # that explicitly so both chunks are warm before exercising the
        # borrow-on-delete path below.
        list(t.list)
        assert list(t.list.delta_list.keys()) == [0, 3]

        del t.list[1]  # remove n1 from the first (full) chunk

        # Chunk 3 was evicted immediately (borrowed-from, so its own
        # offsets are now stale by one) -- check this *before* any further
        # read, since a subsequent read would transparently (and
        # correctly) re-fetch and re-populate it again.
        assert list(t.list.delta_list.keys()) == [0]
        assert t.list.delta_list[0] == [["n0", 1], ["n2", 3], ["n3", 4]]

        assert [r[0] for r in t.list] == ["n0", "n2", "n3", "n4", "n5"]


# ────────────────────────────────────────────────────────────────────────── #
#  remove / pop / insert                                                      #
# ────────────────────────────────────────────────────────────────────────── #

class TestRemovePopInsert:
    def test_remove_finds_row_by_id_even_with_gaps(self, db, table):
        table.list.append(["Alice", 30])
        bob = table.list.append(["Bob", 25])
        table.list.append(["Carol", 40])
        del table.list[0]  # Alice gone; offsets shift, Bob now at position 0

        table.list.remove(bob)  # must still find Bob by ID, not stale offset

        assert [r[0] for r in table.list] == ["Carol"]

    def test_remove_raises_value_error_when_not_found(self, db, table):
        table.list.append(["Alice", 30])
        with pytest.raises(ValueError):
            table.list.remove(["Ghost", 0, 999_999])

    def test_pop_default_removes_last(self, db, table):
        table.list.append(["Alice", 30])
        last = table.list.append(["Bob", 25])
        popped = table.list.pop()
        assert popped == last
        assert [r[0] for r in table.list] == ["Alice"]

    def test_pop_explicit_index(self, db, table):
        first = table.list.append(["Alice", 30])
        table.list.append(["Bob", 25])
        popped = table.list.pop(0)
        assert popped == first
        assert [r[0] for r in table.list] == ["Bob"]

    def test_insert_ignores_index_and_appends(self, db, table):
        table.list.append(["Alice", 30])
        table.list.insert(0, ["Bob", 25])  # index 0 requested...
        assert [r[0] for r in table.list] == ["Alice", "Bob"]  # ...but appended


# ────────────────────────────────────────────────────────────────────────── #
#  append() / extend() -- including chunk-boundary regressions               #
# ────────────────────────────────────────────────────────────────────────── #

class TestAppendExtend:
    def test_append_returns_stored_row_with_id(self, db, table):
        row = table.list.append(["Alice", 30])
        assert row == ["Alice", 30, 1]

    def test_append_updates_length_and_content(self, db, table):
        table.list.append(["Alice", 30])
        table.list.append(["Bob", 25])
        assert len(table.list) == 2
        assert [r[0] for r in table.list] == ["Alice", "Bob"]

    def test_append_emits_a_dbupdates_entry(self, db, table):
        dbupdates[table.id].clear()
        table.list.append(["Alice", 30])
        assert len(dbupdates[table.id]) == 1
        assert dbupdates[table.id][0]["update"] == "add"

    def test_append_failure_returns_none_and_does_not_grow_list(self, db, table):
        result = table.list.append({"no_such_column": "x"})
        assert result is None
        assert len(table.list) == 0

    def test_extend_returns_update_dict_and_grows_list(self, db, table):
        update = table.list.extend([["Alice", 30], ["Bob", 25]])
        assert update["length"] == 2
        assert [r[0] for r in table.list] == ["Alice", "Bob"]

    def test_extend_empty_list(self, db, table):
        update = table.list.extend([])
        assert update["length"] == 0
        assert len(table.list) == 0

    def test_regression_repeated_append_across_chunk_boundary(self, db):
        """
        Regression (severe): append() used to fetch get_delta_chunk(index)
        *after* inserting and unconditionally append the new row to
        whatever it got back. The first time a chunk boundary was crossed
        (e.g. the 4th row at limit=3), that chunk wasn't cached yet, so
        get_delta_chunk did a fresh DB read -- which *already* included
        the row just inserted (the insert commits immediately) -- and then
        the row was appended to it a second time. The result: the new row
        was duplicated in the cache and the true next row silently
        vanished from view, corrupting any table that grew past `limit`
        rows through ordinary table.rows.append() calls (the standard "add
        a row" UI flow).
        """
        t = db.create_table("T", {"name": "TEXT"}, limit=3)
        for i in range(7):
            t.list.append([f"n{i}"])

        names = [r[0] for r in t.list]
        assert names == [f"n{i}" for i in range(7)]  # no duplicates, nothing missing
        assert len(t.list) == 7

    def test_regression_extend_across_a_chunk_boundary_left_by_append(self, db):
        """
        Regression: after the append() fix above, a chunk a plain
        append() loop crosses is deliberately left *uncached* (rather than
        risk fabricating wrong content). extend() then has to cope with
        starting mid-way through such an uncached chunk without assuming
        it's empty -- the original bulk-fill logic did assume that,
        writing the new rows at local offset 0 of that chunk regardless of
        how many pre-existing (uncached) rows were already there,
        misaligning every chunk from that point on.
        """
        t = db.create_table("T", {"name": "TEXT"}, limit=3)
        for i in range(5):  # chunk 0 full [n0,n1,n2]; chunk 3 has n3,n4 but is uncached
            t.list.append([f"n{i}"])

        t.list.extend([["nE"], ["nF"]])

        assert [r[0] for r in t.list] == ["n0", "n1", "n2", "n3", "n4", "nE", "nF"]

    def test_regression_extend_then_extend_again_stays_aligned(self, db):
        t = db.create_table("T", {"name": "TEXT"}, limit=3)
        t.list.extend([["n0"], ["n1"], ["n2"], ["n3"]])  # crosses one boundary already
        t.list.extend([["n4"], ["n5"], ["n6"], ["n7"], ["n8"]])

        assert [r[0] for r in t.list] == [f"n{i}" for i in range(9)]

    @pytest.mark.parametrize("limit", [1, 2, 3, 4])
    def test_regression_append_across_boundary_at_various_limits(self, db, limit):
        t = db.create_table("T", {"name": "TEXT"}, limit=limit)
        for i in range(10):
            t.list.append([f"n{i}"])
        assert [r[0] for r in t.list] == [f"n{i}" for i in range(10)]


# ────────────────────────────────────────────────────────────────────────── #
#  Bypassing Dblist via direct Dbtable calls (self-healing cache)             #
# ────────────────────────────────────────────────────────────────────────── #

class TestDirectDbtableBypass:
    """
    Dbtable.append_row/append_rows/delete_row/delete_rows/clear are public
    methods that a bulk-loading script might call directly instead of going
    through table.list. Dblist detects this via Dbtable._version (bumped by
    each of those methods) and drops its stale chunk cache the next time it
    needs to decide whether that cache can be trusted -- see
    Dblist._sync_cache()'s docstring.
    """

    def test_regression_direct_append_row_does_not_crash_dblist(self, db):
        """
        Regression: calling dbtable.append_row() directly left
        dbtable.list.delta_list holding the pre-insert (too-short, e.g.
        still `{0: []}` for a freshly-created table) cache while
        dbtable.length correctly advanced. The next read through
        dbtable.list computed valid-looking bounds from the *correct*
        length but then indexed into the *stale* chunk, raising
        IndexError instead of transparently reading fresh data.
        """
        t = db.create_table("T", {"name": "TEXT"})
        for i in range(5):
            t.append_row([f"n{i}"])  # bypasses t.list entirely

        assert len(t.list) == 5
        assert [r[0] for r in t.list] == [f"n{i}" for i in range(5)]

    def test_regression_direct_delete_row_is_not_served_stale_afterwards(self, db):
        """Regression: a direct delete_row() call correctly updated
        dbtable.length but never touched dbtable.list's cache, so reads
        through dbtable.list kept returning the deleted row (and omitted
        the real last row) instead of the true, post-delete contents."""
        t = db.create_table("T", {"name": "TEXT"})
        for i in range(5):
            t.list.append([f"n{i}"])

        row_id = t.list[1][-1]  # n1's real DB id
        t.delete_row(row_id)  # bypasses t.list entirely

        assert [r[0] for r in t.list] == ["n0", "n2", "n3", "n4"]

    def test_regression_self_heals_even_when_only_len_was_checked_in_between(self, db):
        """
        Regression: the cache-staleness check originally lived only inside
        get_delta_chunk(), which len() never calls. A direct bypass
        mutation followed by nothing but a len() check (a very natural
        thing to do -- e.g. logging progress) left append()'s "is this
        chunk already cached?" probe looking at a dict key that was
        stale-but-still-present, so it wrongly treated the chunk as known-
        good and hand-appended onto genuinely stale data.
        """
        t = db.create_table("T", {"name": "TEXT"}, limit=1)
        t.list.append(["n0"])
        row_id = t.list[0][-1]

        t.delete_row(row_id)  # direct bypass
        _ = len(t.list)  # only a length check in between -- no indexing at all

        t.list.append(["n1"])  # must not silently merge into the stale chunk 0

        assert [r[0] for r in t.list] == ["n1"]

    def test_direct_append_rows_bulk_bypass(self, db):
        t = db.create_table("T", {"name": "TEXT"}, limit=2)
        t.list.append(["n0"])
        t.append_rows([["n1"], ["n2"], ["n3"]])  # bypasses t.list
        assert [r[0] for r in t.list] == ["n0", "n1", "n2", "n3"]

    def test_direct_clear_bypass(self, db):
        t = db.create_table("T", {"name": "TEXT"})
        t.list.append(["n0"])
        t.list.append(["n1"])
        t.clear()  # bypasses t.list
        assert len(t.list) == 0
        assert list(t.list) == []

    def test_mixed_direct_and_list_mutations_stay_consistent(self, db):
        """A broader smoke test mixing both APIs repeatedly."""
        t = db.create_table("T", {"name": "TEXT"}, limit=2)
        t.list.append(["a"])
        t.append_row(["b"])
        t.list.append(["c"])
        row_id = t.list[0][-1]
        t.delete_row(row_id)
        t.list.append(["d"])
        t.append_rows([["e"], ["f"]])

        assert [r[0] for r in t.list] == ["b", "c", "d", "e", "f"]


# ────────────────────────────────────────────────────────────────────────── #
#  update_cell (graph / relation cell editing)                                #
# ────────────────────────────────────────────────────────────────────────── #

class TestUpdateCell:
    def test_updates_a_node_field_and_persists(self, db, table):
        row = table.list.append(["Alice", 30])
        update = table.list.update_cell(0, 1, 31)  # cell 1 == 'age'
        assert update["data"] == ["Alice", 31, row[-1]]
        assert table.read_rows()[0] == ["Alice", 31, row[-1]]

    def test_updates_the_first_field(self, db, table):
        table.list.append(["Alice", 30])
        table.list.update_cell(0, 0, "Alicia")  # cell 0 == 'name'
        assert table.read_rows()[0][0] == "Alicia"

    def test_cache_mode_updates_db_and_memory_but_returns_none(self, db, table):
        row = table.list.append(["Alice", 30])
        lst = Dblist(table, cache=[["Alice", 30, row[-1]]])

        result = lst.update_cell(0, 1, 99)

        assert result is None  # "no streaming update in cache-mode"
        assert lst[0][1] == 99  # in-memory cache patched
        assert table.read_rows()[0][1] == 99  # and the DB write still happened

    def test_updates_a_relation_field_via_link(self, db):
        """
        update_cell's relation-field branch reads
        self.dbtable.list.link == (link_table, [payload_field_names],
        junction_table_id), which tables.py sets dynamically for
        many-to-many relations (see tables.py's
        `self.rows.link = self.rows.dbtable.link_info`). link[0] (the
        link_table object) is never read by update_cell itself, so a
        placeholder stands in for it here.

        Cells beyond the node columns only exist on rows shaped by
        calc_linked_rows(..., include_rels=True) (node fields + node ID +
        relation payload + relation ID) -- a plain dbtable.list row has no
        such slots, so this uses that shape, matching how tables.py
        actually reassigns self.rows to a linked-rows Dblist for a
        many-to-many filtered Table view.
        """
        orders = db.create_table("Orders", {"item": "TEXT"})
        users = db.create_table("Users", {"name": "TEXT"})
        relname, _ = orders.setup_junction("Users", {"qty": "INTEGER"})
        u = users.append_row(["Alice"])
        o = orders.append_row(["Widget"])
        link = orders.add_link(o[-1], "Users", u[-1], {"qty": 3}, link_index_name=relname)
        link_id = link[-1]

        linked = orders.calc_linked_rows(relname, [u[-1]], "Users", include_rels=True)
        # index2node_relation() always reads self.dbtable.list.link (the
        # canonical live Dblist), regardless of which Dblist update_cell()
        # is actually called on -- tables.py sets it there once at Table
        # construction time and every calc_linked_rows() result shares the
        # same underlying dbtable, so this is where it belongs, not on
        # `linked` itself.
        orders.list.link = (None, ["qty"], relname)
        assert linked[0] == ["Widget", o[-1], 3, link_id]

        # len(table_fields) == 1 ('item') is the implicit node-ID cell;
        # the first relation field ('qty') is the next one after that.
        result = linked.update_cell(0, 2, 9, id=link_id)

        assert result is None  # cache-mode: no streaming update
        assert linked[0] == ["Widget", o[-1], 9, link_id]  # patched in memory
        assert db.qlist(f"SELECT qty FROM [{relname}] WHERE ID = ?", (link_id,)) == [[9]]

    def test_updating_the_implicit_id_cell_raises(self, db):
        """Documents existing behaviour rather than asserting it's ideal:
        the cell index that lands exactly on the row's own (non-editable)
        ID column falls through index2node_relation's relation branch with
        a negative offset, which at_iter always rejects."""
        orders = db.create_table("Orders", {"item": "TEXT"})
        orders.list.link = (None, [], "Orders2Whatever")
        orders.list.append(["Widget"])
        with pytest.raises(IndexError):
            orders.list.update_cell(0, 1, "x")  # cell 1 == len(table_fields) == the ID


# ────────────────────────────────────────────────────────────────────────── #
#  Randomised stress test                                                     #
# ────────────────────────────────────────────────────────────────────────── #

class TestChunkingStress:
    """
    A deterministic, seeded version of the exploratory script used to find
    the append()/extend() chunk-boundary bugs and the direct-Dbtable-bypass
    staleness bug above: run a random sequence of mutations through both
    Dblist's public API and Dbtable's raw API, and after every single step,
    assert the *entire* visible content matches a plain Python list used as
    the reference model. Any future regression in the chunk bookkeeping --
    even one no specific test above happens to cover -- has a good chance
    of showing up here.
    """

    OPS = ["append", "extend", "delete", "direct_append", "direct_extend", "direct_delete"]

    def _run(self, db, seed: int, limit: int, n_ops: int):
        rng = random.Random(seed)
        t = db.create_table("T", {"name": "TEXT"}, limit=limit)
        reference: list[str] = []
        next_id = [0]

        def make_names(k=1):
            names = [f"n{next_id[0] + i}" for i in range(k)]
            next_id[0] += k
            return names

        for op_index in range(n_ops):
            op = rng.choice(self.OPS)
            if op == "append":
                name = make_names()[0]
                t.list.append([name])
                reference.append(name)
            elif op == "extend":
                names = make_names(rng.randint(1, 4))
                t.list.extend([[n] for n in names])
                reference.extend(names)
            elif op == "delete" and reference:
                idx = rng.randrange(len(reference))
                del t.list[idx]
                del reference[idx]
            elif op == "direct_append":
                name = make_names()[0]
                t.append_row([name])
                reference.append(name)
            elif op == "direct_extend":
                names = make_names(rng.randint(1, 4))
                t.append_rows([[n] for n in names])
                reference.extend(names)
            elif op == "direct_delete" and reference:
                idx = rng.randrange(len(reference))
                row_id = t.list[idx][-1]
                t.delete_row(row_id)
                del reference[idx]
            else:
                continue

            actual = [r[0] for r in t.list]
            assert actual == reference, (
                f"seed={seed} limit={limit} op#{op_index}={op}: "
                f"expected {reference}, got {actual}"
            )
            assert len(t.list) == len(reference)

    @pytest.mark.parametrize("limit", [1, 2, 3, 5, 100])
    @pytest.mark.parametrize("seed", range(10))
    def test_random_mutation_sequence_matches_reference_list(self, db, seed, limit):
        self._run(db, seed=seed, limit=limit, n_ops=40)
