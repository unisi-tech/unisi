# Copyright © 2024 UNISI Tech. All rights reserved.
from collections import defaultdict, deque

# storage id -> screen name -> [elem name, block name]
dbshare = defaultdict(lambda: defaultdict(list))

# db id -> deque of pending UI updates.
# Using deque(maxlen=...) prevents unbounded memory growth: old updates are
# silently dropped once the queue is full, which is acceptable because the
# frontend reconciles state on reconnect.
DBUPDATES_MAXLEN = 500
dbupdates: dict[str, deque] = defaultdict(lambda: deque(maxlen=DBUPDATES_MAXLEN))


def at_iter(iterable, times):
    """Return element at position *times* in *iterable*, or raise IndexError."""
    for i, val in enumerate(iterable):
        if i == times:
            return val
    raise IndexError(f"Iterator has no element at index {times}")


class Dblist:
    """
    Lazy paginated proxy-list backed by a Dbtable.

    Data is stored in *chunks* of `limit` rows keyed by their start offset
    (delta_list).  When `cache` is supplied the object is a thin read-only
    wrapper around that in-memory list and never contacts the database.

    Design invariant
    ────────────────
    All DB operations that require a primary key (delete_row, assign_row)
    receive the actual DB row ID extracted from the last element of the row
    array (row[-1]).  The list index (offset) is *never* passed to the DB
    layer directly — doing so would silently corrupt data once gaps appear
    in the ID sequence after deletions.

    Changes vs. original
    ────────────────────
    1.  __iter__ is now a generator so nested loops are fully independent.
    2.  __getitem__ / __setitem__ / __delitem__ support slices and negative
        indices.
    3.  __delitem__ extracts the row ID from the row data and passes that to
        dbtable.delete_row(); the offset is never sent to the DB.
    4.  __setitem__ asserts that the caller is not silently replacing a row's
        ID, which would corrupt the DB.
    5.  remove() correctly looks up the list index by matching row[-1].
    6.  extend() persists the updated length back through dbtable.length.
    7.  dbupdates uses deque(maxlen=DBUPDATES_MAXLEN) to prevent OOM growth.
    8.  cache-mode __setitem__ raises TypeError (read-only by contract).
    """

    def __init__(self, dbtable, init_list=None, cache=None):
        self.cache = cache
        self.dbtable = dbtable          # assigned before self.limit is read
        self.limit = dbtable.limit

        if cache is not None:
            init_list = cache[: self.limit]
        elif init_list is None:
            raise AttributeError("init_list or cache has to be assigned!")

        self.delta_list = {0: init_list}

    # ------------------------------------------------------------------ #
    #  Serialisation helpers                                               #
    # ------------------------------------------------------------------ #

    def get_delta_0(self):
        return self.delta_list[0] if self.cache is None else self.cache[: self.limit]

    def __getstate__(self):
        return dict(length=len(self), limit=self.limit, data=self.get_delta_0())

    def __str__(self):
        return str(self.__getstate__())

    # ------------------------------------------------------------------ #
    #  Iteration                                                           #
    # ------------------------------------------------------------------ #

    def __iter__(self):
        """
        Generator iterator — every call produces a *new* generator object,
        so nested loops (for i in lst: for j in lst: ...) and concurrent
        reads are fully independent.
        """
        for i in range(len(self)):
            yield self[i]

    def __len__(self):
        return len(self.cache) if self.cache is not None else self.dbtable.length

    # ------------------------------------------------------------------ #
    #  Chunk management                                                    #
    # ------------------------------------------------------------------ #

    def get_delta_chunk(self, index: int) -> tuple[int, list]:
        """Return (chunk_start_offset, chunk_list) for the row at *index*.

        Negative indices are normalised before the chunk calculation so that
        ``-1 // limit`` does not produce a negative chunk key.
        """
        if index < 0:
            index = len(self) + index
        if index < 0 or index >= len(self):
            return -1, None

        delta_start = (index // self.limit) * self.limit

        if self.cache is not None:
            return delta_start, self.cache[delta_start: delta_start + self.limit]

        lst = self.delta_list.get(delta_start)
        if lst is None:
            lst = self.dbtable.read_rows(skip=delta_start)
            self.delta_list[delta_start] = lst
        return delta_start, lst

    def clean_cache_from(self, delta_start: int):
        """Evict all cached chunks whose start offset is >= *delta_start*."""
        self.delta_list = {k: v for k, v in self.delta_list.items() if k < delta_start}

    # ------------------------------------------------------------------ #
    #  Element access                                                      #
    # ------------------------------------------------------------------ #

    def __getitem__(self, index):
        """Support positive/negative integers and slices."""
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if self.cache is not None:
            return self.cache[index]
        delta_start, chunk = self.get_delta_chunk(index)
        if chunk is None:
            raise IndexError(f"Dblist index {index} out of range (length={len(self)})")
        if index < 0:
            index = len(self) + index
        return chunk[index - delta_start]

    def __setitem__(self, index, value: list):
        """
        Replace a row in the DB and the local cache.

        Guards:
        - Slices are not supported (raise NotImplementedError).
        - Cache-mode is read-only (raise TypeError).
        - Changing the row ID is forbidden (raise ValueError).  Silently
          allowing it would update the wrong row in the database.
        """
        if isinstance(index, slice):
            raise NotImplementedError("Slice assignment is not supported.")
        if self.cache is not None:
            raise TypeError(
                "Dblist in cache-mode is read-only; use update_cell() instead."
            )
        delta_start, chunk = self.get_delta_chunk(index)
        if chunk is None:
            raise IndexError(f"Dblist index {index} out of range")
        if index < 0:
            index = len(self) + index
        local_idx = index - delta_start
        existing_id = chunk[local_idx][-1]
        if value[-1] != existing_id:
            raise ValueError(
                f"Cannot change row ID: existing={existing_id}, new={value[-1]}. "
                "Changing the primary key would corrupt the database."
            )
        chunk[local_idx] = value
        self.dbtable.assign_row(value)
        update = dict(type="action", update="update", index=index, data=value)
        dbupdates[self.dbtable.id].append(update)

    # ------------------------------------------------------------------ #
    #  Deletion                                                            #
    # ------------------------------------------------------------------ #

    def __delitem__(self, index):
        """
        Delete by positive/negative integer or slice.

        Critical fix: the DB receives the actual primary key extracted from
        ``row[-1]``, never the list offset.  Using the offset would silently
        delete the wrong row once the ID sequence has gaps.
        """
        if isinstance(index, slice):
            # Reverse order keeps smaller indices stable during iteration.
            for i in sorted(range(*index.indices(len(self))), reverse=True):
                del self[i]
            return

        if index < 0:
            index = len(self) + index
        delta_start, chunk = self.get_delta_chunk(index)
        if chunk is None:
            raise IndexError(f"Dblist index {index} out of range")

        local_idx = index - delta_start
        row_id = chunk[local_idx][-1]          # ← actual DB primary key
        self.dbtable.delete_row(row_id)        # ← pass ID, not offset!

        update = dict(type="action", update="delete", index=index, exclude=True)
        dbupdates[self.dbtable.id].append(update)

        del chunk[local_idx]

        limit = self.limit
        next_delta_start = delta_start + limit
        if len(chunk) == limit - 1:
            # Chunk was fully filled; borrow first row from the next chunk.
            next_chunk = self.delta_list.get(next_delta_start)
            if next_chunk:
                chunk.append(next_chunk[0])
            # Chunks beyond the modified one have stale offsets — evict them.
            self.clean_cache_from(next_delta_start)

    # ------------------------------------------------------------------ #
    #  Cell-level update (graph / relation support)                        #
    # ------------------------------------------------------------------ #

    def index2node_relation(self, cell_index: int):
        """Map a column index to ``(is_node_field, field_name)``."""
        table_fields = self.dbtable.table_fields
        delta = cell_index - len(table_fields)
        if delta < 0:
            return True, at_iter(iter(table_fields), cell_index)
        delta -= 1  # skip the implicit ID field
        return False, at_iter(iter(self.dbtable.list.link[1]), delta)

    def update_cell(self, delta: int, cell: int, value, id=None) -> dict | None:
        """
        Update a single cell in the DB and the local cache.
        Returns the update dict, or None for cache-mode lists.
        """
        in_node, field = self.index2node_relation(cell)
        if in_node:
            table_id = self.dbtable.id
            row_id = self[delta][len(self.dbtable.table_fields)]
        else:
            table_id = self.dbtable.list.link[2]
            row_id = id

        self.dbtable.db.update_row(table_id, row_id, {field: value}, in_node)

        if self.cache is not None:
            self.cache[delta][cell] = value
            return None  # no streaming update in cache-mode

        delta_start, chunk = self.get_delta_chunk(delta)
        if chunk is not None:
            chunk[delta - delta_start][cell] = value
        update = dict(type="action", update="update", index=delta, data=self[delta])
        dbupdates[self.dbtable.id].append(update)
        return update

    # ------------------------------------------------------------------ #
    #  Append / extend                                                     #
    # ------------------------------------------------------------------ #

    def append(self, arr):
        """Append a row; return the stored row (including its new DB ID)."""
        row = self.dbtable.append_row(arr)
        if self.cache is not None:
            self.cache.append(row)
            return row

        index = len(self) - 1
        delta_start, lst = self.get_delta_chunk(index)
        if lst is not None:
            lst.append(row)
        update = dict(type="action", update="add", index=index, data=row)
        dbupdates[self.dbtable.id].append(update)
        return row

    def extend(self, rows):
        """
        Bulk-append rows; update local chunk cache and emit a single update.
        dbtable.length is updated atomically inside append_rows().
        """
        delta_start_index = self.dbtable.length   # capture before insert
        rows = self.dbtable.append_rows(rows)      # dbtable.length updated here
        len_rows = len(rows)
        i_rows = 0
        start = delta_start_index

        while len_rows > 0:
            chunk_start = (start // self.limit) * self.limit
            lst = self.delta_list.get(chunk_start)
            if lst is None:
                lst = []
                self.delta_list[chunk_start] = lst
                can_fill = self.limit
            else:
                can_fill = self.limit - len(lst)
            if can_fill:
                lst.extend(rows[i_rows: i_rows + can_fill])
            i_rows += can_fill
            start += can_fill
            len_rows -= can_fill

        delta, data = self.get_delta_chunk(delta_start_index)
        update = dict(
            type="action", update="updates",
            index=delta, data=data, length=self.dbtable.length,
        )
        dbupdates[self.dbtable.id].append(update)
        return update

    # ------------------------------------------------------------------ #
    #  List-compatible helpers                                             #
    # ------------------------------------------------------------------ #

    def insert(self, index, value):
        """
        Relational/graph DBs have no positional insert semantics; we append.
        *index* is accepted for API compatibility but ignored.
        """
        self.append(value)

    def remove(self, value):
        """
        Remove the first row whose DB ID matches ``value[-1]``.

        Fix vs. original: the original did ``del self[value[-1]]`` which
        treated the DB ID as a list offset, silently deleting the wrong row
        when IDs had gaps.
        """
        target_id = value[-1]
        for i, row in enumerate(self):
            if row[-1] == target_id:
                del self[i]
                return
        raise ValueError(f"Row with ID {target_id} not found in Dblist")

    def pop(self, index=-1):
        value = self[index]
        del self[index]
        return value

    def clear(self, detach=False):
        self.dbtable.clear(detach)
        self.delta_list = {0: []}
        dbupdates[self.dbtable.id].append(
            dict(type="action", update="updates", length=0)
        )
